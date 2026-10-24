"""Risk engine: the only component allowed to turn an intent into an order.

Two layers:
  * ``poll``  -- hard, account-wide halts (daily loss, stale data, kill switch)
  * ``check`` -- per-intent rules (size, exposure, duplicates, ...)

A halt is sticky for the trading day: once the daily loss limit is breached no
new order is accepted until ``reset_day`` (or an operator restart). The runtime
also cancels working orders when a halt first appears.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import RiskConfig
from ..events import Order, OrderIntent
from ..wallet import Ledger
from .kill import KillSwitch
from .rules import Action, RiskContext, Rule, default_rules


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    intent: OrderIntent | None = None
    reason: str = ""
    adjusted: bool = False
    halted: bool = False


class RiskEngine:
    def __init__(
        self,
        cfg: RiskConfig,
        wallet: Ledger,
        *,
        symbols: list[str],
        kill: KillSwitch | None = None,
        logger: logging.Logger | None = None,
        rules: list[Rule] | None = None,
    ) -> None:
        self.cfg = cfg
        self.wallet = wallet
        self.kill = kill
        self.log = logger or logging.getLogger("krabs.risk")
        self.rules = rules if rules is not None else default_rules(cfg, symbols)
        self._prices: dict[str, float] = {}
        self._recent: deque[tuple[datetime, OrderIntent]] = deque(maxlen=2000)
        self._daily_halt: str = ""
        self._dynamic_halt: str = ""
        self._events: list[tuple[datetime, str]] = []

    # -- state -----------------------------------------------------------
    @property
    def halted(self) -> bool:
        return bool(self._daily_halt or self._dynamic_halt)

    @property
    def halt_reason(self) -> str:
        return self._daily_halt or self._dynamic_halt

    def note_price(self, symbol: str, price: float) -> None:
        if price > 0:
            self._prices[symbol] = float(price)

    def prices(self) -> dict[str, float]:
        return dict(self._prices)

    def poll(
        self,
        now: datetime,
        *,
        last_event_ts: datetime | None = None,
    ) -> str:
        """Refresh hard halts. Returns the active halt reason ('' if healthy)."""
        dynamic = ""
        if self.kill is not None and self.kill.engaged:
            dynamic = f"kill switch: {self.kill.reason() or 'engaged'}"
        elif (
            self.cfg.max_data_age_s > 0
            and last_event_ts is not None
            and (now - last_event_ts).total_seconds() > self.cfg.max_data_age_s
        ):
            dynamic = (
                f"stale market data: last event {(now - last_event_ts).total_seconds():.0f}s ago "
                f"(max {self.cfg.max_data_age_s:.0f}s)"
            )
        self._dynamic_halt = dynamic

        account = self.wallet.snapshot(now)
        if (
            not self._daily_halt
            and account.day_start_equity > 0
            and account.day_return <= -self.cfg.max_daily_loss_pct
        ):
            self._daily_halt = (
                f"daily loss limit: {account.day_return:.2%} "
                f"(limit -{self.cfg.max_daily_loss_pct:.2%})"
            )
            self.log.error("HALT: %s", self._daily_halt)
            self._events.append((now, self._daily_halt))
        return self.halt_reason

    def reset_day(self, now: datetime) -> None:
        self._daily_halt = ""
        self._dynamic_halt = ""
        self._recent.clear()

    def halt(self, reason: str, now: datetime | None = None) -> None:
        self._dynamic_halt = reason
        if now is not None:
            self._events.append((now, reason))

    def resume(self) -> None:
        """Operator action. Does NOT clear the kill switch file."""
        self._daily_halt = ""
        self._dynamic_halt = ""

    def drain_events(self) -> list[tuple[datetime, str]]:
        events, self._events = self._events, []
        return events

    # -- pre-trade -------------------------------------------------------
    def check(
        self,
        intent: OrderIntent,
        *,
        now: datetime,
        open_orders: list[Order],
        last_event_ts: datetime | None = None,
    ) -> RiskDecision:
        self.poll(now, last_event_ts=last_event_ts)
        if self.halted:
            return RiskDecision(False, None, self.halt_reason, halted=True)

        ctx = RiskContext(
            now=now,
            account=self.wallet.snapshot(now),
            positions=self.wallet.positions(),
            open_orders=open_orders,
            prices=dict(self._prices),
            wallet=self.wallet,
            last_event_ts=last_event_ts,
            recent_intents=list(self._recent),
        )

        current = intent
        adjusted = False
        notes: list[str] = []
        for rule in self.rules:
            result = rule.check(ctx, current)
            if result.action is Action.REJECT:
                self.log.warning(
                    "risk reject %s %s %s: %s",
                    current.side.value,
                    current.symbol,
                    current.qty or current.notional,
                    result.reason,
                )
                return RiskDecision(False, None, result.reason)
            if result.action is Action.ADJUST:
                current = result.intent
                adjusted = True
                notes.append(result.reason)
                ctx.open_orders = open_orders  # unchanged; adjustments are per-intent

        if current.qty is not None and current.qty <= 1e-9:
            return RiskDecision(False, None, "adjusted quantity is zero")

        self._recent.append((now, current))
        cutoff = now - timedelta(days=1)
        while self._recent and self._recent[0][0] < cutoff:
            self._recent.popleft()
        return RiskDecision(
            True, current, reason="; ".join(notes), adjusted=adjusted
        )
