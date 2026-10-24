"""Pre-trade risk rules.

Rules are pure-ish functions over a ``RiskContext``. They may:
  * pass the intent through         -> OK
  * shrink the intent               -> ADJUST (quantity/notional clamp)
  * veto the intent                 -> REJECT
Hard halts (daily loss, stale data, kill switch) are handled by the engine
before rules run, because they must also cancel working orders.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum

from ..events import AccountState, Order, OrderIntent, Position, Side
from ..wallet import Ledger


class Action(str, Enum):
    OK = "ok"
    ADJUST = "adjust"
    REJECT = "reject"


@dataclass(slots=True)
class RiskContext:
    now: datetime
    account: AccountState
    positions: dict[str, Position]
    open_orders: list[Order]
    prices: dict[str, float]
    wallet: Ledger
    last_event_ts: datetime | None = None
    recent_intents: list[tuple[datetime, OrderIntent]] | None = None


@dataclass(slots=True)
class RuleResult:
    action: Action
    intent: OrderIntent
    reason: str = ""


def ok(intent: OrderIntent) -> RuleResult:
    return RuleResult(Action.OK, intent)


def adjust(intent: OrderIntent, reason: str) -> RuleResult:
    return RuleResult(Action.ADJUST, intent, reason)


def reject(reason: str, intent: OrderIntent) -> RuleResult:
    return RuleResult(Action.REJECT, intent, reason)


def reference_price(intent: OrderIntent, ctx: RiskContext) -> float | None:
    for candidate in (intent.limit_price, intent.stop_price):
        if candidate:
            return float(candidate)
    price = ctx.prices.get(intent.symbol)
    return float(price) if price else None


def resolve_qty(intent: OrderIntent, price: float) -> float:
    if intent.qty is not None:
        return float(intent.qty)
    return float(intent.notional or 0.0) / price


def with_qty(intent: OrderIntent, qty: float, reason: str) -> OrderIntent:
    qty = math.floor(qty * 1_000_000) / 1_000_000
    return replace(intent, qty=qty, notional=None)


class Rule:
    def check(self, ctx: RiskContext, intent: OrderIntent) -> RuleResult:  # pragma: no cover
        raise NotImplementedError


class SymbolRule(Rule):
    def __init__(self, symbols: list[str], allowed: list[str]) -> None:
        self.symbols = {s.upper() for s in symbols}
        self.allowed = {s.upper() for s in allowed} if allowed else self.symbols

    def check(self, ctx: RiskContext, intent: OrderIntent) -> RuleResult:
        if intent.symbol not in self.symbols:
            return reject(f"{intent.symbol} is not subscribed in [market].symbols", intent)
        if intent.symbol not in self.allowed:
            return reject(f"{intent.symbol} is not in risk.allowed_symbols", intent)
        if reference_price(intent, ctx) is None:
            return reject(f"no price for {intent.symbol}", intent)
        return ok(intent)


class PriceCollarRule(Rule):
    def __init__(self, max_pct: float) -> None:
        self.max_pct = max_pct

    def check(self, ctx: RiskContext, intent: OrderIntent) -> RuleResult:
        last = ctx.prices.get(intent.symbol)
        if not last:
            return ok(intent)
        for label, price in (("limit", intent.limit_price), ("stop", intent.stop_price)):
            if price and abs(price / last - 1.0) > self.max_pct:
                return reject(
                    f"{label} price {price:.2f} is {abs(price / last - 1):.1%} away from "
                    f"last {last:.2f} (max {self.max_pct:.1%})",
                    intent,
                )
        return ok(intent)


class OrderSizeRule(Rule):
    def __init__(self, max_notional: float) -> None:
        self.max_notional = max_notional

    def check(self, ctx: RiskContext, intent: OrderIntent) -> RuleResult:
        price = reference_price(intent, ctx)
        if price is None:
            return ok(intent)
        notional = intent.notional if intent.notional is not None else resolve_qty(intent, price) * price
        if notional <= self.max_notional:
            return ok(intent)
        if intent.notional is not None:
            return adjust(
                replace(intent, notional=self.max_notional),
                f"clamped notional {notional:,.2f} -> {self.max_notional:,.2f}",
            )
        qty = self.max_notional / price
        return adjust(
            with_qty(intent, qty, "max order notional"),
            f"clamped qty {intent.qty} -> {qty:.6f} (max order notional)",
        )


class PositionLimitRule(Rule):
    def __init__(self, max_position_pct: float) -> None:
        self.max_pct = max_position_pct

    def check(self, ctx: RiskContext, intent: OrderIntent) -> RuleResult:
        price = reference_price(intent, ctx)
        if price is None or ctx.account.equity <= 0:
            return ok(intent)
        cap = ctx.account.equity * self.max_pct
        current = ctx.positions.get(intent.symbol, Position(intent.symbol)).qty
        delta = intent.side.sign * resolve_qty(intent, price)
        projected = current + delta
        cap_qty = cap / price
        allowed = max(-cap_qty, min(cap_qty, projected))
        allowed_delta = allowed - current
        if abs(allowed_delta) < 1e-9:
            return reject(
                f"position limit reached: {intent.symbol} at {current:.4f} shares "
                f"(cap {cap_qty:.4f})",
                intent,
            )
        if allowed_delta * intent.side.sign <= 0:
            return reject(
                f"order would reduce exposure past the {self.max_pct:.0%} position cap",
                intent,
            )
        if abs(allowed_delta - delta) < 1e-9:
            return ok(intent)
        return adjust(
            with_qty(intent, abs(allowed_delta), "position limit"),
            f"clamped qty {abs(delta):.6f} -> {abs(allowed_delta):.6f} "
            f"(max {self.max_pct:.0%} of equity)",
        )


class GrossExposureRule(Rule):
    def __init__(self, max_pct: float) -> None:
        self.max_pct = max_pct

    def check(self, ctx: RiskContext, intent: OrderIntent) -> RuleResult:
        price = reference_price(intent, ctx)
        if price is None or ctx.account.equity <= 0:
            return ok(intent)
        cap = ctx.account.equity * self.max_pct
        deltas: dict[str, float] = {}
        for order in ctx.open_orders:
            px = ctx.prices.get(order.symbol)
            if px:
                deltas[order.symbol] = (
                    deltas.get(order.symbol, 0.0) + order.side.sign * order.remaining * px
                )
        deltas[intent.symbol] = deltas.get(intent.symbol, 0.0) + (
            intent.side.sign * resolve_qty(intent, price) * price
        )

        projected = ctx.account.gross_exposure
        for symbol, delta in deltas.items():
            px = ctx.prices.get(symbol)
            if not px:
                continue
            current_value = ctx.positions.get(symbol, Position(symbol)).qty * px
            projected += abs(current_value + delta) - abs(current_value)
        if projected > cap + 1e-6:
            return reject(
                f"gross exposure would be {projected:,.2f} > {cap:,.2f} "
                f"({self.max_pct:.0%} of equity)",
                intent,
            )
        return ok(intent)


class BuyingPowerRule(Rule):
    def __init__(self, buffer_pct: float = 0.02) -> None:
        self.buffer = buffer_pct

    def check(self, ctx: RiskContext, intent: OrderIntent) -> RuleResult:
        price = reference_price(intent, ctx)
        if price is None:
            return ok(intent)
        qty = resolve_qty(intent, price)
        pending = 0.0
        for order in ctx.open_orders:
            px = ctx.prices.get(order.symbol)
            if px and order.side is Side.SELL:
                covered = max(0.0, ctx.positions.get(order.symbol, Position(order.symbol)).qty)
                pending += min(order.remaining, covered) * px
        ok_afford, reason = ctx.wallet.can_afford(
            intent.symbol, intent.side, qty, price, extra_buying_power=pending
        )
        if not ok_afford:
            return reject(reason, intent)
        if intent.side is Side.BUY:
            required = qty * price
            spendable = (ctx.account.buying_power + pending) * (1.0 - self.buffer)
            if required > spendable:
                return reject(
                    f"buying-power buffer: need {required:,.2f}, "
                    f"spendable {spendable:,.2f} after {self.buffer:.0%} reserve",
                    intent,
                )
        return ok(intent)


class OpenOrdersRule(Rule):
    def __init__(self, max_open_orders: int) -> None:
        self.max_open_orders = max_open_orders

    def check(self, ctx: RiskContext, intent: OrderIntent) -> RuleResult:
        if len(ctx.open_orders) >= self.max_open_orders:
            return reject(
                f"{len(ctx.open_orders)} open orders (max {self.max_open_orders})", intent
            )
        return ok(intent)


class DuplicateRule(Rule):
    def __init__(self, window_s: float) -> None:
        self.window = timedelta(seconds=window_s)

    def check(self, ctx: RiskContext, intent: OrderIntent) -> RuleResult:
        if self.window.total_seconds() <= 0:
            return ok(intent)
        cutoff = ctx.now - self.window
        for ts, previous in ctx.recent_intents or []:
            if ts < cutoff:
                continue
            if (
                previous.symbol == intent.symbol
                and previous.side is intent.side
                and previous.type is intent.type
                and _same_size(previous, intent)
            ):
                return reject(
                    f"duplicate intent for {intent.symbol} {intent.side.value} "
                    f"within {self.window.total_seconds():.0f}s",
                    intent,
                )
        return ok(intent)


def _same_size(a: OrderIntent, b: OrderIntent) -> bool:
    if a.notional is not None or b.notional is not None:
        if a.notional is None or b.notional is None:
            return False
        return abs(a.notional - b.notional) < 1e-6
    return abs((a.qty or 0.0) - (b.qty or 0.0)) < 1e-6


class MinNotionalRule(Rule):
    def __init__(self, minimum: float = 1.0) -> None:
        self.minimum = minimum

    def check(self, ctx: RiskContext, intent: OrderIntent) -> RuleResult:
        price = reference_price(intent, ctx)
        if price is None:
            return ok(intent)
        notional = intent.notional if intent.notional is not None else resolve_qty(intent, price) * price
        if notional < self.minimum:
            return reject(f"order notional {notional:.2f} < minimum {self.minimum:.2f}", intent)
        return ok(intent)


def default_rules(cfg, symbols: list[str]) -> list[Rule]:
    return [
        SymbolRule(symbols, cfg.allowed_symbols),
        PriceCollarRule(cfg.price_collar_pct),
        MinNotionalRule(1.0),
        OrderSizeRule(cfg.max_order_notional),
        PositionLimitRule(cfg.max_position_pct),
        GrossExposureRule(cfg.max_gross_exposure_pct),
        BuyingPowerRule(),
        OpenOrdersRule(cfg.max_open_orders),
        DuplicateRule(cfg.duplicate_window_s),
    ]
