"""Human and machine readable backtest reports."""

from __future__ import annotations

import csv
import json
import math
from collections import Counter
from pathlib import Path

from .backtest.engine import BacktestResult
from .backtest.metrics import round_trips


def render_summary(result: BacktestResult) -> str:
    m = result.metrics
    lines: list[str] = []
    width = 64
    lines.append("=" * width)
    lines.append(f" Krabs backtest  |  {result.strategy}  {_params(result.params)}")
    lines.append(f" run {result.run_id}")
    if result.started_at and result.finished_at:
        lines.append(
            f" {result.started_at:%Y-%m-%d %H:%M} -> {result.finished_at:%Y-%m-%d %H:%M} UTC"
        )
    lines.append("-" * width)
    rows = [
        ("Initial cash", _money(m.get("initial_cash"))),
        ("Final equity", _money(m.get("final_equity"))),
        ("Total return", _pct(m.get("total_return"))),
        ("CAGR", _pct(m.get("cagr"))),
        ("Max drawdown", _pct(m.get("max_drawdown"))),
        ("Sharpe (daily)", _num(m.get("sharpe"))),
        ("Sortino (daily)", _num(m.get("sortino"))),
        ("Volatility (ann.)", _pct(m.get("volatility"))),
        ("Exposure (avg)", _pct(m.get("exposure"))),
        ("Round trips", str(m.get("trades", 0))),
        ("Win rate", _pct(m.get("win_rate"))),
        ("Profit factor", _num(m.get("profit_factor"))),
        ("Avg trade PnL", _money(m.get("avg_trade_pnl"))),
        ("Fees", _money(m.get("fees"))),
        ("Turnover (x equity)", _num(m.get("turnover"))),
        ("Rejected intents", str(len(result.rejections))),
        ("Risk-adjusted orders", str(result.adjusted)),
        ("Bars processed", str(len(result.equity_curve))),
        ("Elapsed", f"{result.elapsed_s:.2f}s"),
    ]
    for label, value in rows:
        lines.append(f" {label:<24}{value:>38}")

    trips = round_trips(result.fills)
    if trips:
        best = sorted(trips, key=lambda t: t.pnl, reverse=True)[:3]
        worst = sorted(trips, key=lambda t: t.pnl)[:3]
        lines.append("-" * width)
        lines.append(" Best round trips")
        for t in best:
            lines.append(
                f"   {t.symbol:<6} {t.qty:>10.4f}  {t.entry:%m-%d %H:%M} -> {t.exit:%m-%d %H:%M}"
                f"  {t.pnl:>+10.2f}"
            )
        lines.append(" Worst round trips")
        for t in worst:
            lines.append(
                f"   {t.symbol:<6} {t.qty:>10.4f}  {t.entry:%m-%d %H:%M} -> {t.exit:%m-%d %H:%M}"
                f"  {t.pnl:>+10.2f}"
            )

    if result.rejections:
        counts = Counter(_short(r.reason, 70) for r in result.rejections)
        lines.append("-" * width)
        lines.append(" Risk rejections (top)")
        for reason, count in counts.most_common(5):
            lines.append(f"   {count:>4}x {reason}")

    lines.append("=" * width)
    return "\n".join(lines)


def write_report(result: BacktestResult, report_dir: str | Path) -> Path:
    out = Path(report_dir) / result.run_id
    out.mkdir(parents=True, exist_ok=True)

    payload = _clean(result.to_dict())
    (out / "result.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (out / "summary.txt").write_text(render_summary(result) + "\n", encoding="utf-8")

    with (out / "equity.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ts", "equity", "cash", "gross_exposure", "drawdown"])
        peak = -math.inf
        for point in result.equity_curve:
            peak = max(peak, point.equity)
            drawdown = point.equity / peak - 1.0 if peak > 0 else 0.0
            writer.writerow(
                [point.ts.isoformat(), f"{point.equity:.2f}", f"{point.cash:.2f}",
                 f"{point.gross:.2f}", f"{drawdown:.6f}"]
            )

    with (out / "trades.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["symbol", "qty", "entry", "exit", "pnl", "return_pct"])
        for t in round_trips(result.fills):
            writer.writerow(
                [t.symbol, f"{t.qty:.6f}", t.entry.isoformat(), t.exit.isoformat(),
                 f"{t.pnl:.2f}", f"{t.return_pct:.6f}"]
            )

    with (out / "orders.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["client_id", "ts", "symbol", "side", "qty", "type", "status",
             "filled_qty", "avg_price", "reason"]
        )
        for o in result.orders:
            writer.writerow(
                [o.client_id, o.updated_at.isoformat(), o.symbol, o.side.value, f"{o.qty:.6f}",
                 o.intent.type.value, o.status.value, f"{o.filled_qty:.6f}",
                 f"{o.avg_price:.4f}", _short(o.intent.reason, 80)]
            )
    return out


def _clean(value):
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def _params(params: dict) -> str:
    return " ".join(f"{k}={v}" for k, v in params.items())


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _money(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return f"{value:,.2f}"


def _pct(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return f"{value * 100:,.2f}%"


def _num(value, digits: int = 2) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return f"{value:,.{digits}f}"
