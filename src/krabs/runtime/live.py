"""Live/paper runtime.

Same loop shape as the backtester, but driven by wall-clock time and a polling
feed. Safety properties that matter more than features:

  * the kill switch is checked every cycle and cancels working orders;
  * a stale feed halts new orders (``risk.max_data_age_s``);
  * after downtime, stale bars are skipped rather than replayed as if live;
  * on startup, any orders left by a previous process are reconciled/cancelled
    so a crash cannot leave orphan orders working at the broker;
  * every decision is persisted before the next one is taken.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..brokers.alpaca import AlpacaBroker
from ..brokers.base import BrokerError, run_prefix
from ..clock import Clock, RealClock
from ..config import Config
from ..events import AccountState, Bar, Fill, Order, OrderIntent
from ..feeds.base import group_bars
from ..market.calendar import EASTERN, bar_is_closed, is_open, next_open
from ..risk import KillSwitch, RiskEngine
from ..strategy.base import Strategy, StrategyContext
from ..wallet import BrokerWallet, Ledger


@dataclass(slots=True)
class RuntimeStats:
    cycles: int = 0
    bars: int = 0
    orders: int = 0
    fills: int = 0
    errors: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    stopped_at: datetime | None = None
    last_bar_ts: datetime | None = None


class TradingRuntime:
    def __init__(
        self,
        cfg: Config,
        strategy: Strategy,
        feed,
        broker,
        wallet: Ledger,
        risk: RiskEngine,
        *,
        run_id: str,
        mode: str,
        kill: KillSwitch,
        store=None,
        clock: Clock | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.cfg = cfg
        self.strategy = strategy
        self.feed = feed
        self.broker = broker
        self.wallet = wallet
        self.risk = risk
        self.run_id = run_id
        self.mode = mode
        self.kill = kill
        self.store = store
        self.clock = clock or RealClock()
        self.log = logger or logging.getLogger("krabs.runtime")
        self.stats = RuntimeStats()

        self._history: dict[str, list[Bar]] = {s: [] for s in cfg.market.symbols}
        self._last_bar_ts: dict[str, datetime] = {}
        self._last_event_ts: datetime | None = None
        self._seen_filled: dict[str, float] = {}
        self._start_equity = cfg.wallet.starting_cash
        self._stop = False
        self._was_halted = False
        self._ctx: StrategyContext | None = None
        self._current_day = None
        self._open_orders_cache: list[Order] = []
        self._seq_prefix = run_prefix(run_id)

    # -- public ----------------------------------------------------------
    async def run(
        self,
        *,
        max_cycles: int | None = None,
        cancel_on_exit: bool = True,
        flatten_on_exit: bool = False,
    ) -> RuntimeStats:
        self._install_signals()
        await self._startup()
        try:
            while not self._stop and (max_cycles is None or self.stats.cycles < max_cycles):
                try:
                    await self._cycle()
                except asyncio.CancelledError:
                    raise
                except BrokerError as exc:
                    # Transport failures must not kill the process: the next
                    # cycle retries, and the risk engine halts on stale data if
                    # the outage persists.
                    self.stats.errors += 1
                    self.log.error("broker error: %s", exc)
                    if self.store is not None:
                        self.store.log_event(self.run_id, "ERROR", "broker", {"error": str(exc)})
                    if self.stats.errors > 20:
                        self.risk.halt("too many consecutive broker errors", self.clock.now())
                except Exception as exc:  # noqa: BLE001 - a strategy bug must not lose state
                    self.stats.errors += 1
                    self.log.exception("unhandled cycle error: %s", exc)
                    if self.store is not None:
                        self.store.log_event(
                            self.run_id, "ERROR", "runtime", {"error": repr(exc)}
                        )
                    self.risk.halt(f"runtime error: {exc!r}", self.clock.now())
                self.stats.cycles += 1
                await self.clock.sleep(max(0.05, self.cfg.feed.poll_seconds))
        finally:
            await self._shutdown(cancel_on_exit=cancel_on_exit, flatten_on_exit=flatten_on_exit)
        return self.stats

    # -- startup / shutdown ------------------------------------------------
    async def _startup(self) -> None:
        now = self.clock.now()
        self.log.info(
            "starting %s run %s strategy=%s kill=%s",
            self.mode,
            self.run_id,
            self.strategy.id,
            self.kill.path,
        )
        if self.store is not None:
            aborted = self.store.abort_stale_runs(exclude_run_id=self.run_id)
            if aborted:
                self.log.warning("marked %d orphaned run(s) as aborted", aborted)
            self.store.start_run(
                run_id=self.run_id,
                mode=self.mode,
                strategy=self.strategy.id,
                params=self.strategy.params,
                config=self.cfg.as_dict(),
                started_at=now,
                initial_cash=self.cfg.wallet.starting_cash,
            )
        if isinstance(self.broker, AlpacaBroker):
            await self._reconcile_startup()
        else:
            self.broker.reconcile()
        await self._warmup(now)
        if not any(self._history.values()):
            raise BrokerError(
                "warmup returned no bars; refusing to trade without market history"
            )
        if hasattr(self.broker, "on_fill"):
            # PaperBroker: report fills into the same pipeline as live fills.
            self.broker.on_fill = self._on_fill
        if hasattr(self.broker, "on_order"):
            self.broker.on_order = self._on_order
        self._start_equity = self.wallet.snapshot(now).equity
        self._ctx = StrategyContext(
            strategy_id=self.strategy.id,
            wallet=self.wallet,
            submit=self._submit,
            bars=lambda symbol: self._history.get(symbol, []),
            now=self.clock.now,
            fractional=self.cfg.wallet.fractional,
            logger=logging.getLogger(f"krabs.strategy.{self.strategy.id}"),
            open_orders=self.broker.open_orders,
        )
        self.strategy.on_start(self._ctx)
        self._heartbeat(now, self.wallet.snapshot(now))

    async def _reconcile_startup(self) -> None:
        """Cancel orphan orders and adopt broker positions before trading."""
        try:
            open_orders = await asyncio.to_thread(self.broker.open_orders)
        except BrokerError as exc:
            raise BrokerError(f"startup reconciliation failed: {exc}") from exc
        if open_orders:
            self.log.warning("cancelling %d orphan order(s) from a previous run", len(open_orders))
            await asyncio.to_thread(self.broker.cancel_all)
        account = await asyncio.to_thread(self.broker.account)
        positions = await asyncio.to_thread(self.broker.positions)
        if isinstance(self.wallet, BrokerWallet):
            self.wallet.refresh(account, positions)
        issues = await asyncio.to_thread(self.broker.reconcile)
        for issue in issues:
            self.log.warning("reconcile: %s", issue)
            if self.store is not None:
                self.store.log_event(self.run_id, "WARNING", "reconcile", {"issue": issue})
        self.log.info(
            "broker account: equity=%.2f cash=%.2f buying_power=%.2f positions=%s",
            account.equity,
            account.cash,
            account.buying_power,
            ", ".join(f"{s}:{p.qty:g}" for s, p in positions.items()) or "none",
        )
        for order in await asyncio.to_thread(self.broker.recent_orders, 200):
            if order.client_id.startswith(self._seq_prefix):
                self._seen_filled[order.client_id] = order.filled_qty
        if self.cfg.wallet.starting_cash and account.equity > 0:
            # In live mode the starting cash is only a local estimate.
            self.wallet.starting_cash = account.equity

    async def _warmup(self, now: datetime) -> None:
        start = now - timedelta(days=self.cfg.market.history_days)
        try:
            bars = await asyncio.to_thread(
                self.feed.history, self.cfg.market.symbols, self.cfg.market.timeframe, start, now
            )
        except Exception as exc:  # noqa: BLE001
            self.log.error("warmup failed: %s", exc)
            if self.store is not None:
                self.store.log_event(self.run_id, "ERROR", "warmup", {"error": repr(exc)})
            return
        bars = [b for b in bars if b.ts <= now]
        limit = max(self.cfg.feed.warmup_bars, 100)
        for bar in bars:
            buf = self._history.setdefault(bar.symbol, [])
            buf.append(bar)
            if len(buf) > limit:
                del buf[: len(buf) - limit]
            self.risk.note_price(bar.symbol, bar.close)
            self._last_bar_ts[bar.symbol] = bar.ts
        if bars:
            self._last_event_ts = max(b.ts for b in bars)
        self.log.info(
            "warmed up %d bars across %d symbol(s), last=%s",
            len(bars),
            len({b.symbol for b in bars}),
            self._last_event_ts.isoformat() if self._last_event_ts else "none",
        )

    async def _shutdown(self, *, cancel_on_exit: bool, flatten_on_exit: bool) -> None:
        self.stats.stopped_at = self.clock.now()
        self.log.info("shutting down %s (cycles=%d)", self.run_id, self.stats.cycles)
        try:
            if flatten_on_exit:
                if hasattr(self.broker, "close_all"):
                    await asyncio.to_thread(self.broker.close_all)
                    self.log.warning("flattened positions on shutdown")
                else:
                    self.log.warning(
                        "paper broker has no real positions to flatten; virtual state is discarded"
                    )
            elif cancel_on_exit:
                await asyncio.to_thread(self.broker.cancel_all)
        except Exception as exc:  # noqa: BLE001
            self.log.error("shutdown cleanup failed: %s", exc)
        try:
            account = self.wallet.snapshot(self.clock.now())
            self._heartbeat(self.clock.now(), account, stopped=True)
            if self.store is not None:
                self.store.finish_run(self.run_id, "stopped", account.equity)
        finally:
            self.log.info("stopped")

    # -- one cycle ---------------------------------------------------------
    async def _cycle(self) -> None:
        now = self.clock.now()
        if not is_open(now) and self.cfg.market.enforce_market_hours:
            self._heartbeat(now, self.wallet.snapshot(now))
            delta = (next_open(now) - now).total_seconds()
            await self.clock.sleep(min(60.0, max(1.0, delta)))
            return

        if isinstance(self.broker, AlpacaBroker):
            await self._sync_broker()

        self._refresh_open_orders()
        if self.kill.engaged:
            if not self._was_halted:
                self.log.error("kill switch engaged: %s", self.kill.reason())
                await asyncio.to_thread(self.broker.cancel_all)
                self._was_halted = True
            await self._after_cycle(now, [], halted=True)
            return

        bars = await asyncio.to_thread(
            self.feed.latest_bars, self.cfg.market.symbols, self.cfg.market.timeframe, 10
        )
        fresh = [
            b
            for b in bars
            if bar_is_closed(b.ts, self.cfg.market.timeframe, now)
            and b.ts > self._last_bar_ts.get(b.symbol, datetime.min.replace(tzinfo=timezone.utc))
        ]
        if not fresh:
            await self._after_cycle(now, [], halted=False)
            return

        newest = max(b.ts for b in fresh)
        age = (now - newest).total_seconds()
        if age > self.cfg.risk.max_data_age_s:
            # We were down (or the feed broke). Do not trade on history.
            self.log.warning("skipping %d stale bar(s); newest is %.0fs old", len(fresh), age)
            for bar in fresh:
                self._last_bar_ts[bar.symbol] = bar.ts
            await self._after_cycle(now, [], halted=True)
            return

        processed: list[Bar] = []
        for chunk in group_bars(fresh, self.cfg.market.timeframe):
            ts = chunk[0].ts
            day = ts.astimezone(EASTERN).date()
            if day != self._current_day and self._ctx is not None:
                if self._current_day is not None:
                    self.wallet.reset_day(ts)
                    self.risk.reset_day(ts)
                    self.log.info("new trading day: %s", day)
                self._current_day = day
                self.strategy.on_day_start(self._ctx, day)

            for bar in chunk:
                if not isinstance(self.broker, AlpacaBroker):
                    self.broker.on_event(bar)
                self.risk.note_price(bar.symbol, bar.close)
                buf = self._history.setdefault(bar.symbol, [])
                buf.append(bar)
                if len(buf) > max(self.cfg.feed.warmup_bars, 100):
                    del buf[0]
                self._last_bar_ts[bar.symbol] = bar.ts
                self._last_event_ts = bar.ts
                self.stats.bars += 1
                processed.append(bar)

            self.risk.poll(now, last_event_ts=self._last_event_ts)
            if self.risk.halted and not self._was_halted:
                self.log.error("risk halt: %s", self.risk.halt_reason)
                await asyncio.to_thread(self.broker.cancel_all)
                self._was_halted = True
            if self._ctx is not None and not self.risk.halted:
                for bar in chunk:
                    self.strategy.on_bar(self._ctx, bar)

        if self.risk.halted:
            self.log.error("risk halt: %s", self.risk.halt_reason)
        else:
            self._was_halted = False
        await self._after_cycle(now, processed, halted=self.risk.halted)

    async def _after_cycle(self, now: datetime, processed: list[Bar], *, halted: bool) -> None:
        account = self.wallet.snapshot(now)
        if self.store is not None:
            self.store.record_equity(self.run_id, account)
            for event_ts, reason in self.risk.drain_events():
                self.store.log_event(self.run_id, "ERROR", "risk", {"reason": reason})
        self._heartbeat(now, account, halted=halted)
        if processed:
            self.log.info(
                "cycle: %d bar(s) %s..%s equity=%.2f halted=%s",
                len(processed),
                processed[0].ts.isoformat(timespec="minutes"),
                processed[-1].ts.isoformat(timespec="minutes"),
                account.equity,
                halted,
            )

    # -- broker sync -------------------------------------------------------
    async def _sync_broker(self) -> None:
        account = await asyncio.to_thread(self.broker.account)
        positions = await asyncio.to_thread(self.broker.positions)
        if isinstance(self.wallet, BrokerWallet):
            self.wallet.refresh(account, positions)
        else:
            self.wallet.mark_all({s: p.market_price for s, p in positions.items()})

        orders = await asyncio.to_thread(self.broker.recent_orders, 200)
        for order in orders:
            if not order.client_id.startswith(self._seq_prefix):
                continue
            previous = self._seen_filled.get(order.client_id, 0.0)
            delta = order.filled_qty - previous
            if delta > 1e-9:
                self._seen_filled[order.client_id] = order.filled_qty
                # Alpaca reports a cumulative average fill price; for partial
                # fills this is an approximation of the incremental price.
                fill = Fill(
                    order_id=order.client_id,
                    symbol=order.symbol,
                    side=order.side,
                    qty=delta,
                    price=order.avg_price or self.risk.prices().get(order.symbol, 0.0),
                    ts=order.updated_at,
                    commission=0.0,
                    strategy_id=order.intent.strategy_id,
                )
                self._on_fill(fill)

    def _on_order(self, order: Order) -> None:
        if self.store is not None:
            self.store.record_order(self.run_id, order)

    def _on_fill(self, fill: Fill) -> None:
        self.stats.fills += 1
        if self.store is not None:
            self.store.record_fill(self.run_id, fill)
        if self._ctx is not None:
            self.strategy.on_fill(self._ctx, fill)

    def _refresh_open_orders(self) -> None:
        try:
            self._open_orders_cache = self.broker.open_orders()
        except BrokerError:
            # Keep the previous snapshot; risk will halt if this persists.
            pass

    # -- order entry -------------------------------------------------------
    def _submit(self, intent: OrderIntent) -> Order | None:
        now = self.clock.now()
        working = [o for o in self._open_orders_cache if o.is_open]
        decision = self.risk.check(
            intent,
            now=now,
            open_orders=working,
            last_event_ts=self._last_event_ts,
        )
        if not decision.approved or decision.intent is None:
            self.log.warning(
                "rejected %s %s %s: %s",
                intent.side.value,
                intent.symbol,
                intent.qty or intent.notional,
                decision.reason,
            )
            if self.store is not None:
                self.store.log_event(
                    self.run_id,
                    "WARNING",
                    "risk.reject",
                    {"symbol": intent.symbol, "side": intent.side.value, "reason": decision.reason},
                )
            return None
        try:
            order = self.broker.submit(decision.intent)
        except BrokerError as exc:
            self.stats.errors += 1
            self.log.error("submit failed: %s", exc)
            if self.store is not None:
                self.store.log_event(self.run_id, "ERROR", "order", {"error": str(exc)})
            return None
        self.stats.orders += 1
        self._open_orders_cache.append(order)
        if self.store is not None:
            self.store.record_order(self.run_id, order)
        if order.status.value == "rejected":
            self.log.error("broker rejected %s: %s", order.client_id, order.reject_reason)
        return order

    # -- observability -----------------------------------------------------
    def _heartbeat(
        self, now: datetime, account: AccountState, *, halted: bool = False, stopped: bool = False
    ) -> None:
        payload = {
            "ts": now.isoformat(),
            "mode": self.mode,
            "run_id": self.run_id,
            "strategy": self.strategy.id,
            "pid": os.getpid(),
            "cycles": self.stats.cycles,
            "bars": self.stats.bars,
            "orders": self.stats.orders,
            "fills": self.stats.fills,
            "errors": self.stats.errors,
            "halted": halted,
            "halt_reason": self.risk.halt_reason,
            "kill_switch": self.kill.engaged,
            "equity": round(account.equity, 2),
            "cash": round(account.cash, 2),
            "buying_power": round(account.buying_power, 2),
            "start_equity": round(self._start_equity, 2),
            "day_start_equity": round(account.day_start_equity, 2),
            "day_pnl": round(account.day_pnl, 2),
            "day_return": account.day_return,
            "gross_exposure": round(account.gross_exposure, 2),
            "long_value": round(account.long_value, 2),
            "short_value": round(account.short_value, 2),
            "realized_pnl": round(account.realized_pnl, 2),
            "unrealized_pnl": round(account.unrealized_pnl, 2),
            "positions": {
                s: {
                    "qty": p.qty,
                    "avg": p.avg_price,
                    "last": p.market_price,
                    "value": round(p.market_value, 2),
                    "unrealized": round(p.unrealized_pnl, 2),
                }
                for s, p in self.wallet.positions().items()
            },
            "last_bar": self._last_event_ts.isoformat() if self._last_event_ts else None,
            "strategy_diagnostics": self.strategy.diagnostics(),
            "stopped": stopped,
        }
        path = Path(self.cfg.app.data_dir) / "heartbeat.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)

    # -- signals -----------------------------------------------------------
    def _install_signals(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self._request_stop, sig.name)
            if hasattr(signal, "SIGUSR1"):
                loop.add_signal_handler(signal.SIGUSR1, self._on_usr1)
        except (NotImplementedError, RuntimeError, ValueError):
            self.log.debug("signal handlers unavailable in this context")

    def _request_stop(self, name: str = "") -> None:
        self.log.warning("received %s: stopping after this cycle", name)
        self._stop = True

    def _on_usr1(self) -> None:
        self.log.error("SIGUSR1: engaging kill switch")
        self.kill.engage("SIGUSR1")
