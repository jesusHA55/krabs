"""Backtest engine: historical bars -> simulated fills -> metrics.

The loop is intentionally the same shape as the live runtime: market event in,
strategy out, risk gate, broker execute, persist. Only the clock and the feed
differ. If a strategy behaves differently here and live, it is a bug in the
strategy (usually look-ahead), not in the engine.

Execution semantics: an intent created while processing bar ``t`` is submitted
after the broker has already seen bar ``t``, so it can only fill on bar ``t+1``.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

from ..clock import SimulatedClock
from ..config import Config
from ..events import Bar, Fill, Order, OrderIntent, OrderStatus
from ..market.calendar import EASTERN
from ..risk import RiskEngine
from ..strategy.base import Strategy, StrategyContext
from ..wallet import PaperWallet
from ..brokers.paper import FillModel, PaperBroker
from .metrics import EquityPoint, compute_metrics


@dataclass(frozen=True, slots=True)
class Rejection:
    ts: datetime
    intent: OrderIntent
    reason: str


@dataclass(slots=True)
class BacktestResult:
    run_id: str
    strategy: str
    params: dict
    mode: str
    started_at: datetime | None
    finished_at: datetime | None
    initial_cash: float
    final_equity: float
    equity_curve: list[EquityPoint]
    orders: list[Order]
    fills: list[Fill]
    rejections: list[Rejection]
    metrics: dict
    config: dict
    elapsed_s: float = 0.0
    adjusted: int = 0

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "strategy": self.strategy,
            "params": self.params,
            "mode": self.mode,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "initial_cash": self.initial_cash,
            "final_equity": self.final_equity,
            "metrics": self.metrics,
            "elapsed_s": round(self.elapsed_s, 3),
            "counts": {
                "orders": len(self.orders),
                "fills": len(self.fills),
                "rejections": len(self.rejections),
                "bars": len(self.equity_curve),
                "adjusted": self.adjusted,
            },
            "config": self.config,
        }


class BacktestEngine:
    def __init__(
        self,
        cfg: Config,
        strategy: Strategy,
        feed,
        *,
        store=None,
        run_id: str | None = None,
        logger: logging.Logger | None = None,
        max_history: int | None = None,
    ) -> None:
        self.cfg = cfg
        self.strategy = strategy
        self.feed = feed
        self.store = store
        self.log = logger or logging.getLogger("krabs.backtest")
        self.run_id = run_id or f"bt-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
        self.clock = SimulatedClock(datetime(2000, 1, 1, tzinfo=timezone.utc))
        self.wallet = PaperWallet(
            cfg.wallet.starting_cash,
            currency=cfg.wallet.currency,
            allow_short=cfg.wallet.allow_short,
            margin_multiplier=cfg.wallet.margin_multiplier,
        )
        self.broker = PaperBroker(
            self.wallet,
            self.clock,
            FillModel(
                slippage_bps=cfg.execution.slippage_bps,
                half_spread_bps=cfg.execution.half_spread_bps,
                commission_per_share=cfg.execution.commission_per_share,
                min_commission=cfg.execution.min_commission,
                max_participation=cfg.execution.max_participation,
                allow_partial_fills=cfg.execution.allow_partial_fills,
                spread_bps_by_symbol=cfg.execution.spread_costs(),
            ),
            delay_bars=cfg.execution.delay_bars,
            run_id=self.run_id,
            logger=self.log,
            on_fill=self._on_fill,
            on_order=self._on_order,
        )
        self.risk = RiskEngine(
            cfg.risk,
            self.wallet,
            symbols=cfg.market.symbols,
            logger=self.log,
        )
        self._history: dict[str, deque[Bar]] = {
            symbol: deque(maxlen=max_history or max(1000, cfg.feed.warmup_bars * 2))
            for symbol in cfg.market.symbols
        }
        self._orders: dict[str, Order] = {}
        self.fills: list[Fill] = []
        self.rejections: list[Rejection] = []
        self.adjusted = 0
        self._last_event_ts: datetime | None = None
        self._ctx: StrategyContext | None = None
        self._borrow_paid = 0.0

    # -- public ----------------------------------------------------------
    def run(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
        *,
        warmup_bars: int = 0,
    ) -> BacktestResult:
        t0 = time.perf_counter()
        ctx = self._make_context()
        self._ctx = ctx
        if start is not None and warmup_bars > 0:
            self._prefill_history(start, int(warmup_bars))
        if self.store is not None:
            self.store.start_run(
                run_id=self.run_id,
                mode="backtest",
                strategy=self.strategy.id,
                params=self.strategy.params,
                config=self.cfg.as_dict(),
                started_at=datetime.now(timezone.utc),
                initial_cash=self.cfg.wallet.starting_cash,
            )

        self.strategy.on_start(ctx)
        points: list[EquityPoint] = []
        current_day = None
        bars_seen = 0

        for chunk in self.feed.bars(self.cfg.market.symbols, self.cfg.market.timeframe, start, end):
            if not chunk:
                continue
            ts = chunk[0].ts
            self.clock.set(ts)
            day = ts.astimezone(EASTERN).date()
            if day != current_day:
                if current_day is not None:
                    self._charge_borrow(ts)
                    self.wallet.reset_day(ts)
                    self.risk.reset_day(ts)
                current_day = day
                self.strategy.on_day_start(ctx, day)

            # 1) Market data first: fills resting orders at this bar, marks closes.
            for bar in chunk:
                self.broker.on_event(bar)
                self._last_event_ts = bar.ts
                self.risk.note_price(bar.symbol, bar.close)
                self._history.setdefault(bar.symbol, deque(maxlen=1000)).append(bar)
                bars_seen += 1

            # 2) Hard risk checks (daily loss, stale data) before new orders.
            self.risk.poll(ts, last_event_ts=self._last_event_ts)

            # 3) Strategy reacts to completed bars. A halted account accepts no
            #    orders, so there is nothing useful for on_bar to do.
            if not self.risk.halted:
                for bar in chunk:
                    self.clock.set(bar.ts)
                    self.strategy.on_bar(ctx, bar)

            account = self.wallet.snapshot(ts)
            points.append(
                EquityPoint(
                    ts=ts,
                    equity=account.equity,
                    cash=account.cash,
                    gross=account.gross_exposure,
                )
            )
            if self.store is not None:
                self.store.record_equity(self.run_id, account)

        self.strategy.on_end(ctx)
        final = self.wallet.snapshot(self.clock.now())
        metrics = compute_metrics(points, self.fills, self.cfg.wallet.starting_cash)
        metrics["borrow_cost"] = self._borrow_paid
        elapsed = time.perf_counter() - t0

        result = BacktestResult(
            run_id=self.run_id,
            strategy=self.strategy.id,
            params=dict(self.strategy.params),
            mode="backtest",
            started_at=points[0].ts if points else None,
            finished_at=points[-1].ts if points else None,
            initial_cash=self.cfg.wallet.starting_cash,
            final_equity=final.equity,
            equity_curve=points,
            orders=list(self._orders.values()),
            fills=list(self.fills),
            rejections=list(self.rejections),
            metrics=metrics,
            config=self.cfg.as_dict(),
            elapsed_s=elapsed,
            adjusted=self.adjusted,
        )
        self.log.info(
            "backtest %s: %d bars, %d fills, %d rejections, final %.2f (%.2f%%) in %.2fs",
            self.strategy.id,
            bars_seen,
            len(self.fills),
            len(self.rejections),
            final.equity,
            metrics.get("total_return", 0.0) * 100,
            elapsed,
        )
        if self.store is not None:
            self.store.finish_run(self.run_id, "finished", final.equity)
            for ts, reason in self.risk.drain_events():
                self.store.log_event(self.run_id, "ERROR", "risk", {"reason": reason})
        return result

    def _charge_borrow(self, ts: datetime) -> None:
        """Charge one day of short borrow against the previous close marks."""
        rate = float(self.cfg.execution.borrow_bps_per_year)
        if rate <= 0:
            return
        prices = {
            symbol: history[-1].close
            for symbol, history in self._history.items()
            if history
        }
        if not prices:
            return
        self.wallet.mark_all(prices)
        short_value = abs(self.wallet.snapshot(ts).short_value)
        if short_value <= 0:
            return
        amount = short_value * (rate / 10_000.0) / 252.0
        charged = self.wallet.charge(amount, ts, "short borrow")
        self._borrow_paid += charged

    def _prefill_history(self, start: datetime, warmup_bars: int) -> None:
        """Load history before ``start`` without trading or recording metrics.

        Strategies read ``ctx.bars``/``ctx.history`` for screening and model
        warm-up. Without this, a fold or holdout starts blind and loses its
        first formation window; with it, the evaluation window starts warm
        while the wallet, fills and equity curve still begin at ``start``.
        """
        limit = max(1, int(warmup_bars))
        buffers: dict[str, deque[Bar]] = {
            symbol: deque(maxlen=limit) for symbol in self.cfg.market.symbols
        }
        for chunk in self.feed.bars(
            self.cfg.market.symbols, self.cfg.market.timeframe, None, start
        ):
            for bar in chunk:
                if bar.ts >= start:
                    continue
                buffer = buffers.get(bar.symbol)
                if buffer is not None:
                    buffer.append(bar)
        latest: datetime | None = None
        loaded = 0
        for symbol, buffer in buffers.items():
            if not buffer:
                continue
            target = self._history.setdefault(symbol, deque(maxlen=max(1000, limit)))
            target.extend(buffer)
            loaded += len(buffer)
            if latest is None or buffer[-1].ts > latest:
                latest = buffer[-1].ts
        if latest is not None:
            self.clock.set(latest)
        self.log.debug(
            "warm start: %d bars across %d symbols before %s",
            loaded,
            len(buffers),
            start,
        )

    # -- wiring ----------------------------------------------------------
    def _make_context(self) -> StrategyContext:
        return StrategyContext(
            strategy_id=self.strategy.id,
            wallet=self.wallet,
            submit=self._submit,
            bars=lambda symbol: self._history.get(symbol, ()),
            now=self.clock.now,
            fractional=self.cfg.wallet.fractional,
            logger=logging.getLogger(f"krabs.strategy.{self.strategy.id}"),
            open_orders=self.broker.open_orders,
        )

    def _submit(self, intent: OrderIntent) -> Order | None:
        now = self.clock.now()
        decision = self.risk.check(
            intent,
            now=now,
            open_orders=self.broker.open_orders(),
            last_event_ts=self._last_event_ts,
        )
        if not decision.approved or decision.intent is None:
            self.rejections.append(Rejection(now, intent, decision.reason))
            if self.store is not None:
                self.store.log_event(
                    self.run_id,
                    "WARNING",
                    "risk.reject",
                    {"intent": _intent_dict(intent), "reason": decision.reason},
                )
            return None
        if decision.adjusted:
            self.adjusted += 1
            self.log.debug("risk adjusted: %s", decision.reason)
            if self.store is not None:
                self.store.log_event(
                    self.run_id, "INFO", "risk.adjust", {"reason": decision.reason}
                )
        return self.broker.submit(decision.intent)

    def _on_fill(self, fill: Fill) -> None:
        self.fills.append(fill)
        if self.store is not None:
            self.store.record_fill(self.run_id, fill)
        if self._ctx is not None:
            self.strategy.on_fill(self._ctx, fill)

    def _on_order(self, order: Order) -> None:
        self._orders[order.client_id] = order
        if self.store is not None:
            self.store.record_order(self.run_id, order)
        if order.status is OrderStatus.REJECTED:
            self.rejections.append(
                Rejection(order.updated_at, order.intent, order.reject_reason)
            )


def _intent_dict(intent: OrderIntent) -> dict:
    return {
        "symbol": intent.symbol,
        "side": intent.side.value,
        "qty": intent.qty,
        "notional": intent.notional,
        "type": intent.type.value,
        "limit_price": intent.limit_price,
        "reason": intent.reason,
    }
