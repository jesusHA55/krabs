"""Local paper broker: the simulated execution venue.

Design notes
------------
* **No look-ahead.** Orders submitted while processing the bar at ``t`` can
  only fill on the *next* market event. A strategy that sees a close cannot
  get that same close as its fill price.
* Fills happen on the next bar's open (market orders) or when the bar/quote
  crosses a resting limit/stop. Slippage and half-spread are applied
  adversely; limit fills never exceed the limit.
* Volume participation caps partial fills so a backtest cannot assume infinite
  liquidity. Set ``max_participation = 0`` to disable the cap.
* This is a *simulator*, not an exchange. Queue position, latency, halts and
  auction mechanics are out of scope; Alpaca's paper account is the next step
  up in fidelity.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from ..clock import Clock
from ..events import (
    AccountState,
    Bar,
    Fill,
    MarketEvent,
    Order,
    OrderIntent,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    Side,
    TimeInForce,
    Trade,
)
from ..wallet import Ledger


@dataclass(slots=True)
class FillModel:
    slippage_bps: float = 1.0
    half_spread_bps: float = 1.5
    commission_per_share: float = 0.0
    min_commission: float = 0.0
    max_participation: float = 0.25
    allow_partial_fills: bool = True
    # Full quoted spread per symbol in bps; half of it is charged per fill.
    # Symbols absent here fall back to ``half_spread_bps`` above.
    spread_bps_by_symbol: dict[str, float] = field(default_factory=dict)

    def half_spread_for(self, symbol: str) -> float:
        quoted = self.spread_bps_by_symbol.get(symbol.upper())
        return quoted / 2.0 if quoted is not None else self.half_spread_bps

    def commission(self, qty: float) -> float:
        if qty <= 0:
            return 0.0
        return max(self.min_commission, self.commission_per_share * qty)


class PaperBroker:
    def __init__(
        self,
        wallet: Ledger,
        clock: Clock,
        fill_model: FillModel | None = None,
        *,
        run_id: str = "paper",
        logger: logging.Logger | None = None,
        on_fill: Callable[[Fill], None] | None = None,
        on_order: Callable[[Order], None] | None = None,
    ) -> None:
        self.wallet = wallet
        self.clock = clock
        self.fill = fill_model or FillModel()
        self.run_id = run_id
        self.log = logger or logging.getLogger("krabs.broker.paper")
        self.on_fill = on_fill
        self.on_order = on_order
        self._orders: "OrderedDict[str, Order]" = OrderedDict()
        self._open: "OrderedDict[str, Order]" = OrderedDict()
        self._seq = 0
        self._last_price: dict[str, float] = {}
        self._now: datetime = clock.now()
        self._volume_used: dict[tuple[str, datetime], float] = {}

    # -- execution port ---------------------------------------------------
    def submit(self, intent: OrderIntent) -> Order:
        self._seq += 1
        order = Order(
            client_id=f"paper-{self._seq:08d}",
            intent=intent,
            qty=0.0,
            created_at=self.clock.now(),
        )
        price = self._reference_price(intent)
        if price is None:
            return self._reject(order, f"no reference price for {intent.symbol}")

        try:
            qty = intent.qty if intent.qty is not None else float(intent.notional or 0) / price
        except ZeroDivisionError:
            return self._reject(order, "reference price is zero")
        if qty <= 0:
            return self._reject(order, "resolved quantity is zero")
        order.qty = qty

        ok, reason = self.wallet.can_afford(
            intent.symbol,
            intent.side,
            qty,
            price,
            extra_buying_power=self._pending_proceeds(),
        )
        if not ok:
            return self._reject(order, reason)

        self._orders[order.client_id] = order
        self._open[order.client_id] = order
        if self.on_order:
            self.on_order(order)
        return order

    def cancel(self, client_id: str) -> bool:
        order = self._open.pop(client_id, None)
        if order is None:
            return False
        order.status = OrderStatus.CANCELED
        order.updated_at = self.clock.now()
        if self.on_order:
            self.on_order(order)
        return True

    def cancel_all(self, symbol: str | None = None) -> int:
        targets = [
            oid for oid, order in self._open.items() if symbol is None or order.symbol == symbol
        ]
        for oid in targets:
            self.cancel(oid)
        return len(targets)

    def get_order(self, client_id: str) -> Order | None:
        return self._orders.get(client_id)

    def open_orders(self) -> list[Order]:
        return list(self._open.values())

    def account(self) -> AccountState:
        return self.wallet.snapshot(self._now)

    def positions(self) -> dict[str, Position]:
        return self.wallet.positions()

    # -- market data ------------------------------------------------------
    def on_event(self, event: MarketEvent) -> None:
        if event.ts > self._now:
            self._now = event.ts
        self._last_price[event.symbol] = self._event_price(event)

        if isinstance(event, Bar):
            self._on_bar(event)
        elif isinstance(event, Quote):
            self._on_quote(event)
        elif isinstance(event, Trade):
            self._on_trade(event)

    # -- internals --------------------------------------------------------
    def _on_bar(self, bar: Bar) -> None:
        for order in list(self._open.values()):
            if order.symbol != bar.symbol:
                continue
            self._try_fill_bar(order, bar)
        self.wallet.mark(bar.symbol, bar.close)

    def _on_quote(self, quote: Quote) -> None:
        for order in list(self._open.values()):
            if order.symbol != quote.symbol:
                continue
            self._try_fill_quote(order, quote)
        self.wallet.mark(quote.symbol, (quote.bid + quote.ask) / 2.0)

    def _on_trade(self, trade: Trade) -> None:
        for order in list(self._open.values()):
            if order.symbol != trade.symbol:
                continue
            if order.intent.is_market:
                self._execute(order, order.remaining, trade.price, trade.ts, slippage=True)
        self.wallet.mark(trade.symbol, trade.price)

    def _try_fill_bar(self, order: Order, bar: Bar) -> None:
        intent = order.intent
        remaining = order.remaining
        if remaining <= 0:
            self._finalize(order)
            return

        if intent.type is OrderType.MARKET:
            qty = self._bar_qty(order, bar)
            if qty > 0:
                self._execute(order, qty, bar.open, bar.ts, slippage=True)
            self._finish_tif(order)
            return

        triggered = order.triggered or self._stop_triggered(intent, bar)
        if triggered and not order.triggered:
            order.triggered = True
            if intent.type is OrderType.STOP:
                stop = float(intent.stop_price or 0)
                if intent.side is Side.BUY:
                    price = max(bar.open, stop)
                else:
                    price = min(bar.open, stop)
                qty = self._bar_qty(order, bar)
                if qty > 0:
                    self._execute(order, qty, price, bar.ts, slippage=True)
                self._finish_tif(order)
                return

        if intent.type is OrderType.STOP and order.triggered:
            qty = self._bar_qty(order, bar)
            if qty > 0:
                self._execute(order, qty, bar.open, bar.ts, slippage=True)
            self._finish_tif(order)
            return

        if intent.type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and triggered:
            limit = float(intent.limit_price or 0)
            crosses = (
                bar.low <= limit if intent.side is Side.BUY else bar.high >= limit
            )
            if crosses:
                price = min(limit, bar.open) if intent.side is Side.BUY else max(limit, bar.open)
                qty = self._bar_qty(order, bar)
                if qty > 0:
                    self._execute(order, qty, price, bar.ts, slippage=False)
        self._finish_tif(order)

    def _try_fill_quote(self, order: Order, quote: Quote) -> None:
        intent = order.intent
        remaining = order.remaining
        if remaining <= 0:
            self._finalize(order)
            return
        if intent.type is OrderType.MARKET:
            price = quote.ask if intent.side is Side.BUY else quote.bid
            self._execute(order, remaining, price, quote.ts, slippage=True)
        elif intent.type is OrderType.STOP and not order.triggered:
            stop = float(intent.stop_price or 0)
            hit = quote.ask >= stop if intent.side is Side.BUY else quote.bid <= stop
            if hit:
                order.triggered = True
                self._execute(order, remaining, stop, quote.ts, slippage=True)
        elif intent.type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and (
            order.triggered or self._stop_triggered(intent, quote)
        ):
            order.triggered = True
            limit = float(intent.limit_price or 0)
            hit = quote.ask <= limit if intent.side is Side.BUY else quote.bid >= limit
            if hit:
                price = quote.ask if intent.side is Side.BUY else quote.bid
                self._execute(order, remaining, price, quote.ts, slippage=False)
        self._finish_tif(order)

    def _execute(
        self,
        order: Order,
        qty: float,
        price: float,
        ts: datetime,
        *,
        slippage: bool,
    ) -> None:
        qty = min(qty, order.remaining)
        if qty <= 1e-12:
            return
        if slippage:
            half = self.fill.half_spread_for(order.symbol)
            bps = (self.fill.slippage_bps + half) / 10_000.0
            price = price * (1.0 + bps) if order.side is Side.BUY else price * (1.0 - bps)
        price = round(price, 4)
        commission = self.fill.commission(qty)
        fill = Fill(
            order_id=order.client_id,
            symbol=order.symbol,
            side=order.side,
            qty=qty,
            price=price,
            ts=ts,
            commission=commission,
            strategy_id=order.intent.strategy_id,
            tag=order.intent.tag,
        )
        self.wallet.apply_fill(fill)
        total = order.filled_qty + qty
        order.avg_price = (order.avg_price * order.filled_qty + price * qty) / total
        order.filled_qty = total
        order.commission += commission
        order.updated_at = ts
        order.status = (
            OrderStatus.FILLED if order.remaining <= 1e-9 else OrderStatus.PARTIALLY_FILLED
        )
        self.log.debug(
            "fill %s %s %.6f @ %.4f (%s)", order.side.value, order.symbol, qty, price, order.client_id
        )
        if self.on_fill:
            self.on_fill(fill)
        if order.status is OrderStatus.FILLED:
            self._open.pop(order.client_id, None)
        if self.on_order:
            self.on_order(order)

    def _bar_qty(self, order: Order, bar: Bar) -> float:
        """Apply the volume participation cap; returns 0 if nothing can fill now."""
        remaining = order.remaining
        if self.fill.max_participation <= 0 or not self.fill.allow_partial_fills:
            return remaining
        key = (bar.symbol, bar.ts)
        available = max(0.0, bar.volume * self.fill.max_participation - self._volume_used.get(key, 0.0))
        qty = min(remaining, available)
        if qty <= 1e-9:
            return 0.0
        if order.intent.tif is TimeInForce.FOK and qty < remaining - 1e-9:
            return 0.0
        self._volume_used[key] = self._volume_used.get(key, 0.0) + qty
        if len(self._volume_used) > 50_000:  # long backtests: keep memory bounded
            self._volume_used.clear()
        return qty

    @staticmethod
    def _stop_triggered(intent: OrderIntent, event: Bar | Quote) -> bool:
        if intent.type not in (OrderType.STOP, OrderType.STOP_LIMIT):
            return True
        stop = float(intent.stop_price or 0)
        if isinstance(event, Bar):
            return event.high >= stop if intent.side is Side.BUY else event.low <= stop
        return event.ask >= stop if intent.side is Side.BUY else event.bid <= stop

    def _finish_tif(self, order: Order) -> None:
        if order.intent.tif in (TimeInForce.IOC, TimeInForce.FOK) and order.remaining > 1e-9:
            order.status = OrderStatus.CANCELED
            order.updated_at = self._now
            self._open.pop(order.client_id, None)
            if self.on_order:
                self.on_order(order)

    def _finalize(self, order: Order) -> None:
        if order.remaining <= 1e-9:
            if order.status is not OrderStatus.FILLED:
                order.status = OrderStatus.FILLED
            self._open.pop(order.client_id, None)

    def _reject(self, order: Order, reason: str) -> Order:
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        order.updated_at = self.clock.now()
        self._orders[order.client_id] = order
        self.log.warning("rejected %s: %s", order.intent, reason)
        if self.on_order:
            self.on_order(order)
        return order

    def _reference_price(self, intent: OrderIntent) -> float | None:
        for candidate in (intent.limit_price, intent.stop_price):
            if candidate:
                return float(candidate)
        return self._last_price.get(intent.symbol)

    @staticmethod
    def _event_price(event: MarketEvent) -> float:
        if isinstance(event, Bar):
            return event.close
        if isinstance(event, Quote):
            return (event.bid + event.ask) / 2.0
        return event.price

    def reconcile(self) -> list[str]:
        """Local simulator: local state is the truth, so nothing to reconcile."""
        return []

    def _pending_proceeds(self) -> float:
        """Cash that resting sell orders will free on the next event.

        This lets a strategy rotate from A into B within one bar instead of
        waiting an extra bar for the sell to fill, while still refusing genuine
        over-allocation.
        """
        total = 0.0
        for order in self._open.values():
            if order.side is not Side.SELL:
                continue
            price = self._last_price.get(order.symbol, 0.0)
            covered = max(0.0, self.wallet.position(order.symbol).qty)
            total += min(order.remaining, covered) * price
        return total
