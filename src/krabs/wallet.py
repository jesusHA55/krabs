"""Wallet implementations: simulated ledger and broker-mirrored account.

A ``Wallet`` is an *accounting* concern, not an execution concern:

  * ``PaperWallet`` owns cash and positions; ``PaperBroker`` mutates it on fills.
  * ``BrokerWallet`` mirrors Alpaca's account/positions; the broker is the
    source of truth and local fills are only optimistic estimates until the
    next reconciliation.

Keeping these separate is what lets the same strategy run against a simulated
wallet, Alpaca's paper account, or real money without code changes.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from .events import AccountState, Fill, Position, Side, utcnow
from .ports import Wallet


class Ledger:
    """Double-entry-ish cash + position accounting for simulated trading."""

    def __init__(
        self,
        starting_cash: float,
        *,
        currency: str = "USD",
        allow_short: bool = False,
        margin_multiplier: float = 1.0,
    ) -> None:
        if starting_cash <= 0:
            raise ValueError("starting_cash must be positive")
        self.starting_cash = float(starting_cash)
        self.currency = currency
        self.allow_short = allow_short
        self.margin_multiplier = max(1.0, margin_multiplier)
        self._cash = float(starting_cash)
        self._positions: dict[str, Position] = {}
        self._realized = 0.0
        self._last_ts: datetime = utcnow()
        self.day_start_equity = float(starting_cash)

    # -- marks and snapshots --------------------------------------------
    def mark(self, symbol: str, price: float) -> None:
        if price <= 0:
            return
        pos = self._positions.get(symbol)
        if pos is not None and not pos.is_flat:
            pos.market_price = float(price)

    def mark_all(self, prices: dict[str, float]) -> None:
        for symbol, price in prices.items():
            self.mark(symbol, price)

    def snapshot(self, ts: datetime | None = None) -> AccountState:
        if ts is not None:
            self._last_ts = ts
        long_value = 0.0
        short_value = 0.0
        unrealized = 0.0
        for pos in self._positions.values():
            if pos.is_flat:
                continue
            value = pos.market_value
            unrealized += pos.unrealized_pnl
            if value >= 0:
                long_value += value
            else:
                short_value += value
        equity = self._cash + long_value + short_value
        gross = long_value + abs(short_value)
        buying_power = max(0.0, equity * self.margin_multiplier - gross)
        return AccountState(
            ts=self._last_ts,
            cash=self._cash,
            equity=equity,
            buying_power=buying_power,
            long_value=long_value,
            short_value=short_value,
            realized_pnl=self._realized,
            unrealized_pnl=unrealized,
            day_start_equity=self.day_start_equity,
            currency=self.currency,
        )

    def positions(self) -> dict[str, Position]:
        return {s: p for s, p in self._positions.items() if not p.is_flat}

    def position(self, symbol: str) -> Position:
        return self._positions.get(symbol, Position(symbol))

    def reset_day(self, ts: datetime | None = None) -> None:
        """Roll the daily loss baseline. Called by the engine at session change."""
        self.day_start_equity = self.snapshot(ts).equity

    # -- accounting ------------------------------------------------------
    def apply_fill(self, fill: Fill) -> None:
        if fill.qty <= 0:
            raise ValueError("fill qty must be positive")
        signed = fill.side.sign * fill.qty
        self._cash += fill.cash_delta
        self._last_ts = fill.ts

        pos = self._positions.setdefault(fill.symbol, Position(fill.symbol))
        prev = pos.qty
        if abs(prev) < 1e-12:
            pos.avg_price = fill.price
            pos.qty = signed
        elif (prev > 0) == (signed > 0):
            # Adding to an existing position: weighted average cost.
            total = abs(prev) + abs(signed)
            pos.avg_price = (abs(prev) * pos.avg_price + abs(signed) * fill.price) / total
            pos.qty = prev + signed
        else:
            # Reducing, closing or flipping.
            closed = min(abs(prev), abs(signed))
            self._realized += (fill.price - pos.avg_price) * closed * (1 if prev > 0 else -1)
            new_qty = prev + signed
            if abs(new_qty) < 1e-12:
                new_qty = 0.0
                pos.avg_price = 0.0
            elif (new_qty > 0) != (prev > 0):
                pos.avg_price = fill.price
            pos.qty = new_qty
        pos.market_price = fill.price
        if pos.is_flat:
            self._positions.pop(fill.symbol, None)

    # -- pre-trade checks -------------------------------------------------
    def can_afford(
        self,
        symbol: str,
        side: Side,
        qty: float,
        price: float,
        extra_buying_power: float = 0.0,
    ) -> tuple[bool, str]:
        if qty <= 0 or price <= 0:
            return False, "invalid quantity or price"
        notional = qty * price
        pos = self.position(symbol)
        state = self.snapshot()
        available = state.buying_power + max(0.0, extra_buying_power)
        if side is Side.BUY:
            if notional > available + 1e-6:
                return False, (
                    f"insufficient buying power: need {notional:,.2f}, "
                    f"have {available:,.2f}"
                )
            return True, ""
        # Selling: closing a long is always allowed.
        if pos.qty >= qty - 1e-9:
            return True, ""
        short_qty = qty - max(pos.qty, 0.0)
        if not self.allow_short:
            return False, f"short selling disabled (would short {short_qty:.4f} {symbol})"
        short_notional = short_qty * price
        if short_notional > available + 1e-6:
            return False, (
                f"insufficient margin for short: need {short_notional:,.2f}, "
                f"have {available:,.2f}"
            )
        return True, ""


class PaperWallet(Ledger):
    """Simulated wallet used by the local matching engine and backtests."""


class BrokerWallet(Ledger):
    """Wallet that mirrors a live broker account.

    ``refresh`` replaces local state with broker truth. It must be called on
    every runtime cycle and during startup reconciliation.
    """

    def __init__(self, starting_cash: float = 1.0, **kwargs) -> None:
        super().__init__(starting_cash, **kwargs)
        self._account: AccountState | None = None

    def refresh(self, account: AccountState, positions: dict[str, Position]) -> None:
        self._account = account
        self._cash = account.cash
        self._realized = account.realized_pnl
        self._last_ts = account.ts
        self._positions = {
            symbol: Position(
                symbol=symbol,
                qty=pos.qty,
                avg_price=pos.avg_price,
                market_price=pos.market_price or pos.avg_price,
            )
            for symbol, pos in positions.items()
            if not pos.is_flat
        }

    def snapshot(self, ts: datetime | None = None) -> AccountState:
        if self._account is None:
            return super().snapshot(ts)
        return self._account if ts is None else replace(self._account, ts=ts)


def build_wallet(cfg, *, broker_backed: bool = False) -> Wallet:
    """Factory used by the CLI/runtime. Keeps mode->wallet wiring in one place."""
    kwargs = dict(
        currency=cfg.wallet.currency,
        allow_short=cfg.wallet.allow_short,
        margin_multiplier=cfg.wallet.margin_multiplier,
    )
    if broker_backed:
        return BrokerWallet(cfg.wallet.starting_cash, **kwargs)
    return PaperWallet(cfg.wallet.starting_cash, **kwargs)
