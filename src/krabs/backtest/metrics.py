"""Performance metrics computed from an equity curve and fill log."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from ..events import Fill
from ..indicators import max_drawdown, realized_volatility
from ..market.calendar import EASTERN

TRADING_DAYS = 252


@dataclass(frozen=True, slots=True)
class EquityPoint:
    ts: datetime
    equity: float
    cash: float
    gross: float


@dataclass(frozen=True, slots=True)
class RoundTrip:
    symbol: str
    qty: float
    entry: datetime
    exit: datetime
    pnl: float
    return_pct: float


def daily_closes(points: Sequence[EquityPoint]) -> list[float]:
    """Last equity mark per US trading date."""
    by_day: dict = {}
    for point in points:
        by_day[point.ts.astimezone(EASTERN).date()] = point.equity
    return [by_day[day] for day in sorted(by_day)]


def sharpe(returns: Sequence[float], periods_per_year: int = TRADING_DAYS) -> float:
    if len(returns) < 2:
        return math.nan
    mean = statistics.fmean(returns)
    stdev = statistics.stdev(returns)
    if stdev == 0:
        return math.nan
    return mean / stdev * math.sqrt(periods_per_year)


def sortino(returns: Sequence[float], periods_per_year: int = TRADING_DAYS) -> float:
    if len(returns) < 2:
        return math.nan
    mean = statistics.fmean(returns)
    downside = [r for r in returns if r < 0]
    if not downside:
        return math.nan
    dd = math.sqrt(sum(r * r for r in downside) / len(downside))
    if dd == 0:
        return math.nan
    return mean / dd * math.sqrt(periods_per_year)


def round_trips(fills: Sequence[Fill]) -> list[RoundTrip]:
    """Average-cost round trips per symbol, including commissions in PnL."""
    book: dict[str, dict[str, float | datetime]] = {}
    trips: list[RoundTrip] = []
    for fill in sorted(fills, key=lambda f: f.ts):
        state = book.setdefault(fill.symbol, {"qty": 0.0, "avg": 0.0, "entry_ts": fill.ts})
        qty = float(state["qty"])
        signed = fill.side.sign * fill.qty
        if qty == 0 or (qty > 0) == (signed > 0):
            total = abs(qty) + abs(signed)
            state["avg"] = (abs(qty) * float(state["avg"]) + abs(signed) * fill.price) / total
            state["qty"] = qty + signed
            if qty == 0:
                state["entry_ts"] = fill.ts
        else:
            closed = min(abs(qty), abs(signed))
            entry = float(state["avg"])
            pnl = (fill.price - entry) * closed * (1 if qty > 0 else -1) - fill.commission
            cost_basis = entry * closed
            trips.append(
                RoundTrip(
                    symbol=fill.symbol,
                    qty=closed,
                    entry=state["entry_ts"],  # type: ignore[arg-type]
                    exit=fill.ts,
                    pnl=pnl,
                    return_pct=pnl / cost_basis if cost_basis else 0.0,
                )
            )
            new_qty = qty + signed
            if abs(new_qty) < 1e-12:
                state["qty"] = 0.0
                state["avg"] = 0.0
                state["entry_ts"] = fill.ts
            elif (new_qty > 0) != (qty > 0):
                state["qty"] = new_qty
                state["avg"] = fill.price
                state["entry_ts"] = fill.ts
            else:
                state["qty"] = new_qty
    return trips


def compute_metrics(
    points: Sequence[EquityPoint],
    fills: Sequence[Fill],
    initial_cash: float,
) -> dict[str, float | int | str | None]:
    if not points:
        return {"final_equity": initial_cash, "total_return": 0.0}
    equity = [p.equity for p in points]
    final = equity[-1]
    total_return = final / initial_cash - 1.0

    days = daily_closes(points)
    daily_returns = [days[i] / days[i - 1] - 1.0 for i in range(1, len(days)) if days[i - 1] > 0]
    years = (points[-1].ts - points[0].ts).total_seconds() / (365.25 * 24 * 3600)
    # Annualising a few days of returns produces meaningless (and sometimes
    # overflowing) numbers; only report CAGR for runs of at least a month.
    cagr = None
    if years >= 30 / 365.25 and final > 0:
        try:
            cagr = math.exp(math.log(final / initial_cash) / years) - 1.0
        except (OverflowError, ValueError):
            cagr = None

    trips = round_trips(fills)
    wins = [t.pnl for t in trips if t.pnl > 0]
    losses = [t.pnl for t in trips if t.pnl <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    exposure = statistics.fmean(
        p.gross / p.equity if p.equity > 0 else 0.0 for p in points
    )

    return {
        "initial_cash": round(initial_cash, 2),
        "final_equity": round(final, 2),
        "total_return": total_return,
        "cagr": cagr,
        "max_drawdown": max_drawdown(equity),
        "sharpe": sharpe(daily_returns),
        "sortino": sortino(daily_returns),
        "volatility": realized_volatility(daily_returns, TRADING_DAYS),
        "exposure": exposure,
        "trades": len(trips),
        "win_rate": (len(wins) / len(trips)) if trips else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "avg_trade_pnl": (sum(t.pnl for t in trips) / len(trips)) if trips else None,
        "fees": round(sum(f.commission for f in fills), 2),
        "turnover": round(
            sum(f.notional for f in fills) / statistics.fmean(equity), 4
        )
        if equity
        else 0.0,
    }
