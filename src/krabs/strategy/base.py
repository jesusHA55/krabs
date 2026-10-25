"""Strategy API.

A strategy sees a ``StrategyContext`` and nothing else: no sockets, no broker
clients, no wall clock. That is what makes the same code testable in a
backtest, runnable against a paper wallet, and deployable with real money.
"""

from __future__ import annotations

import logging
import math
from abc import ABC
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, ClassVar, Sequence

from ..events import (
    AccountState,
    Bar,
    Fill,
    Order,
    OrderIntent,
    OrderType,
    Position,
    Side,
    TimeInForce,
)
from ..indicators import atr, ema, rsi, sma
from ..ports import Wallet
from .params import deep_merge, expand_paths, get_path, unknown_paths

Submitter = Callable[[OrderIntent], "Order | None"]


class StrategyError(RuntimeError):
    pass


class StrategyContext:
    """The only surface a strategy touches."""

    def __init__(
        self,
        *,
        strategy_id: str,
        wallet: Wallet,
        submit: Submitter,
        bars: Callable[[str], Sequence[Bar]],
        now: Callable[[], datetime],
        fractional: bool = True,
        min_trade_notional: float = 1.0,
        logger: logging.Logger | None = None,
        open_orders: Callable[[], Sequence[Order]] | None = None,
    ) -> None:
        self.strategy_id = strategy_id
        self.wallet = wallet
        self._submit = submit
        self._bars = bars
        self._now = now
        self.fractional = fractional
        self.min_trade_notional = min_trade_notional
        self.log = logger or logging.getLogger(f"krabs.strategy.{strategy_id}")
        self._open_orders = open_orders or (lambda: ())

    # -- state -----------------------------------------------------------
    @property
    def now(self) -> datetime:
        return self._now()

    def snapshot(self) -> AccountState:
        return self.wallet.snapshot()

    @property
    def equity(self) -> float:
        return self.wallet.snapshot().equity

    @property
    def cash(self) -> float:
        return self.wallet.snapshot().cash

    def position(self, symbol: str) -> Position:
        return self.wallet.position(symbol.upper())

    def positions(self) -> dict[str, Position]:
        return self.wallet.positions()

    def open_orders(self) -> list[Order]:
        """Orders accepted but not yet filled, as seen by the broker."""
        return list(self._open_orders())

    def has_open_order(self, symbol: str | None = None) -> bool:
        wanted = symbol.upper() if symbol else None
        return any(
            wanted is None or order.symbol == wanted for order in self.open_orders()
        )

    # -- market data -----------------------------------------------------
    def bars(self, symbol: str, n: int = 2) -> list[Bar]:
        rows = list(self._bars(symbol.upper()))
        return rows[-n:] if n > 0 else rows

    def bar(self, symbol: str) -> Bar:
        rows = self.bars(symbol, 1)
        if not rows:
            raise StrategyError(f"no bars available for {symbol}")
        return rows[-1]

    def price(self, symbol: str) -> float:
        return self.bar(symbol).close

    def history(self, symbol: str, n: int = 50, field: str = "close") -> list[float]:
        rows = self.bars(symbol, n)
        try:
            return [float(getattr(bar, field)) for bar in rows]
        except AttributeError as exc:
            raise StrategyError(f"unknown bar field: {field}") from exc

    def sma(self, symbol: str, period: int, field: str = "close") -> float:
        values = self.history(symbol, max(period, 2), field)
        return sma(values, period)[-1]

    def ema(self, symbol: str, period: int, field: str = "close") -> float:
        values = self.history(symbol, max(period * 3, period + 1), field)
        return ema(values, period)[-1]

    def rsi(self, symbol: str, period: int = 14) -> float:
        values = self.history(symbol, max(period * 4, period + 2), "close")
        return rsi(values, period)[-1]

    def atr(self, symbol: str, period: int = 14) -> float:
        n = max(period * 3, period + 2)
        bars = self.bars(symbol, n)
        return atr(
            [b.high for b in bars], [b.low for b in bars], [b.close for b in bars], period
        )[-1]

    # -- order entry -----------------------------------------------------
    def buy(
        self,
        symbol: str,
        qty: float | None = None,
        *,
        notional: float | None = None,
        type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        stop_price: float | None = None,
        tif: TimeInForce = TimeInForce.DAY,
        reason: str = "",
        tag: str = "",
    ) -> Order | None:
        return self._order(
            Side.BUY, symbol, qty, notional, type, limit_price, stop_price, tif, reason, tag
        )

    def sell(
        self,
        symbol: str,
        qty: float | None = None,
        *,
        notional: float | None = None,
        type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        stop_price: float | None = None,
        tif: TimeInForce = TimeInForce.DAY,
        reason: str = "",
        tag: str = "",
    ) -> Order | None:
        return self._order(
            Side.SELL, symbol, qty, notional, type, limit_price, stop_price, tif, reason, tag
        )

    def _order(
        self,
        side: Side,
        symbol: str,
        qty: float | None,
        notional: float | None,
        type: OrderType,
        limit_price: float | None,
        stop_price: float | None,
        tif: TimeInForce,
        reason: str,
        tag: str,
    ) -> Order | None:
        symbol = symbol.upper()
        if qty is not None:
            qty = self._round_qty(qty)
            if qty <= 0:
                return None
        if notional is not None and notional < self.min_trade_notional:
            return None
        intent = OrderIntent(
            symbol=symbol,
            side=side,
            qty=qty,
            notional=notional,
            type=type,
            limit_price=limit_price,
            stop_price=stop_price,
            tif=tif,
            strategy_id=self.strategy_id,
            reason=reason,
            tag=tag,
        )
        return self._submit(intent)

    def _round_qty(self, qty: float) -> float:
        if self.fractional:
            return round(qty, 6)
        return float(math.floor(qty + 1e-9))

    def target_weight(
        self,
        symbol: str,
        weight: float,
        *,
        price: float | None = None,
        reason: str = "",
    ) -> Order | None:
        """Rebalance ``symbol`` to ``weight`` of current equity.

        ``weight`` is per position, not per portfolio. Negative weights require
        short selling to be enabled in the wallet.
        """
        symbol = symbol.upper()
        if weight < 0 and not getattr(self.wallet, "allow_short", False):
            raise StrategyError("negative target weight requires allow_short")
        px = price if price is not None else self.price(symbol)
        if px <= 0:
            raise StrategyError(f"invalid price for {symbol}: {px}")
        equity = self.equity
        target_value = weight * equity
        current_value = self.position(symbol).qty * px
        delta = target_value - current_value
        if abs(delta) < self.min_trade_notional:
            return None
        qty = self._round_qty(abs(delta) / px)
        if qty <= 0:
            return None
        if delta > 0:
            return self.buy(symbol, qty=qty, reason=reason or f"target {weight:.0%}")
        return self.sell(symbol, qty=qty, reason=reason or f"target {weight:.0%}")

    def close(self, symbol: str, reason: str = "close") -> Order | None:
        pos = self.position(symbol)
        if pos.is_flat:
            return None
        if pos.qty > 0:
            return self.sell(symbol, qty=abs(pos.qty), reason=reason)
        return self.buy(symbol, qty=abs(pos.qty), reason=reason)

    def flatten(self, reason: str = "flatten") -> list[Order]:
        orders = []
        for symbol in list(self.positions()):
            order = self.close(symbol, reason=reason)
            if order is not None:
                orders.append(order)
        return orders


class Strategy(ABC):
    """Base class. Subclasses implement ``on_bar`` (and optionally hooks)."""

    id: ClassVar[str] = "strategy"
    description: ClassVar[str] = ""
    defaults: ClassVar[dict[str, object]] = {}

    def __init__(self, **params: object) -> None:
        expanded = expand_paths(params)
        unknown = unknown_paths(expanded, self.defaults)
        if unknown:
            raise ValueError(f"unknown parameter(s) for {self.id}: {sorted(unknown)}")
        self.params: dict[str, object] = deep_merge(self.defaults, expanded)

    def p(self, name: str):
        return get_path(self.params, name)

    def on_start(self, ctx: StrategyContext) -> None:
        """Called once before the first market event."""

    def on_bar(self, ctx: StrategyContext, bar: Bar) -> None:
        """Called for every completed bar, chronologically, one symbol at a time."""

    def on_quote(self, ctx: StrategyContext, quote) -> None:
        """Optional: only used by event-driven live feeds."""

    def on_fill(self, ctx: StrategyContext, fill: Fill) -> None:
        """Called after a fill is confirmed by the wallet/broker."""

    def on_day_start(self, ctx: StrategyContext, day: date) -> None:
        """Called once per trading day before the first bar."""

    def on_end(self, ctx: StrategyContext) -> None:
        """Called once after the last event."""

    def diagnostics(self) -> dict[str, Any]:
        """Optional structured state for the heartbeat/dashboard."""
        return {}

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.params})"


@dataclass(frozen=True, slots=True)
class StrategyInfo:
    id: str
    description: str
    defaults: dict[str, object]
    cls: type[Strategy]
