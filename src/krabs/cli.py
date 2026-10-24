"""Krabs command line interface.

    krabs backtest -c configs/example.toml
    krabs paper    -c configs/alpaca-paper.toml
    krabs live     -c configs/live.toml --i-understand-the-risk
    krabs kill | krabs status | krabs doctor | krabs strategies
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import logging
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from .backtest import BacktestEngine
from .brokers.alpaca import AlpacaBroker, AlpacaClient, trading_base_url
from .brokers.base import BrokerError
from .brokers.paper import FillModel, PaperBroker
from .clock import RealClock
from .config import TIMEFRAMES, Config, ConfigError
from .dashboard import DashboardData, serve as serve_dashboard
from .doctor import print_checks, run_checks
from .feeds.alpaca import AlpacaFeed
from .feeds.csv import CsvFeed
from .feeds.synthetic import SyntheticFeed
from .feeds.synthetic_live import SyntheticLiveFeed
from .optimize import (
    StrategyOptimizer,
    load_bars,
    parse_space,
    render_optimization_summary,
    write_optimization_report,
)
from .risk import KillSwitch, RiskEngine
from .runtime import TradingRuntime
from .store import SqliteStore
from .strategy import build, coerce_params, describe, load_builtin
from .strategy.params import deep_merge, flatten
from .strategy.registry import get as get_strategy
from .feeds.base import InMemoryFeed
from .wallet import BrokerWallet, PaperWallet

LOG = logging.getLogger("krabs")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-c", "--config", help="path to TOML config")
    parser.add_argument("--log-level", help="override log level (DEBUG, INFO, WARNING, ...)")
    parser.add_argument("--json-logs", action="store_true", help="structured JSON logs")


def _common_parent() -> argparse.ArgumentParser:
    """Allow -c after the subcommand too: `krabs backtest -c file.toml`."""
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("-c", "--config", default=argparse.SUPPRESS, help="path to TOML config")
    parent.add_argument("--log-level", default=argparse.SUPPRESS, help="override log level")
    parent.add_argument("--json-logs", action="store_true", default=argparse.SUPPRESS,
                        help="structured JSON logs")
    return parent


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="krabs",
        description="Algorithmic trading on a Raspberry Pi: backtest, paper, live.",
    )
    _add_common(parser)
    parser.add_argument("--version", action="version", version=f"krabs {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    bt = sub.add_parser(
        "backtest", help="replay historical bars through the paper broker", parents=[_common_parent()]
    )
    bt.add_argument("--strategy", help="strategy id (see `krabs strategies`)")
    bt.add_argument(
        "--param", action="append", default=[], metavar="KEY=VALUE", help="override a strategy parameter"
    )
    bt.add_argument("--days", type=int, help="synthetic period / history lookback in days")
    bt.add_argument("--timeframe", choices=TIMEFRAMES, help="override [market].timeframe")
    bt.add_argument(
        "--symbols",
        help="comma-separated symbols; default: all symbols found in --csv",
    )
    bt.add_argument("--seed", type=int, help="synthetic data seed")
    bt.add_argument("--csv", help="use a CSV file instead of synthetic data")
    bt.add_argument("--report-dir", help="override [backtest].report_dir")
    bt.add_argument("--store", action="store_true", help="persist the run to SQLite")
    bt.add_argument("--quiet", action="store_true", help="only print the report path")

    for name, help_text in (
        ("paper", "live market data + local simulated wallet"),
        ("broker-paper", "live market data + Alpaca paper account"),
        ("live", "REAL MONEY. Requires --i-understand-the-risk"),
    ):
        run = sub.add_parser(name, help=help_text, parents=[_common_parent()])
        run.add_argument("--strategy", help="strategy id")
        run.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
        run.add_argument("--max-cycles", type=int, default=0, help=argparse.SUPPRESS)
        run.add_argument("--flatten-on-exit", action="store_true", help="liquidate positions on SIGTERM")
        if name == "paper":
            run.add_argument(
                "--offline",
                action="store_true",
                help="synthetic bars, no broker/API keys, no network (deployment test)",
            )
        if name == "live":
            run.add_argument(
                "--i-understand-the-risk",
                action="store_true",
                help="acknowledge that this trades real money",
            )

    opt = sub.add_parser(
        "optimize",
        help="search strategy parameters against an objective (with holdout)",
        parents=[_common_parent()],
    )
    opt.add_argument("--strategy", help="strategy id (see `krabs strategies`)")
    opt.add_argument(
        "--space",
        action="append",
        required=True,
        metavar="NAME=LOW:HIGH[:STEP]",
        help="parameter range, or NAME=a,b,c for categorical values",
    )
    opt.add_argument("--method", choices=("random", "grid", "de"), default="random")
    opt.add_argument("--trials", type=int, default=40, help="evaluation budget")
    opt.add_argument("--folds", type=int, default=4, help="contiguous in-sample folds")
    opt.add_argument("--holdout", type=float, default=0.2, help="final untouched fraction")
    opt.add_argument(
        "--objective",
        choices=("sharpe", "sortino", "total_return", "calmar", "profit_factor"),
        default="sharpe",
    )
    opt.add_argument("--min-trades", type=int, default=1)
    opt.add_argument("--penalty", type=float, default=0.5, help="fold dispersion penalty")
    opt.add_argument("--dd-penalty", type=float, default=0.5, help="drawdown penalty")
    opt.add_argument("--turnover-penalty", type=float, default=0.01)
    opt.add_argument("--seed", type=int, default=7)
    opt.add_argument("--days", type=int, help="synthetic/lookback days")
    opt.add_argument("--timeframe", choices=TIMEFRAMES)
    opt.add_argument("--symbols", help="comma-separated symbols")
    opt.add_argument("--csv", help="CSV file (symbols auto-detected)")
    opt.add_argument("--report-dir", help="override [backtest].report_dir")
    opt.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    opt.add_argument("--top", type=int, default=10, help="rows in the printed table")

    dash = sub.add_parser(
        "dashboard",
        help="serve the mobile-friendly monitoring dashboard",
        parents=[_common_parent()],
    )
    dash.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    dash.add_argument("--port", type=int, default=8787)
    dash.add_argument("--token", help="dashboard token (default: generate/read var/dashboard.token)")
    dash.add_argument("--token-file", help="token file path")
    dash.add_argument("--no-auth", action="store_true", help="disable token auth (loopback only)")
    dash.add_argument(
        "--allow-controls",
        action="store_true",
        help="allow the dashboard to engage/clear the kill switch (read-only otherwise)",
    )
    dash.add_argument("--show-token", action="store_true", help="print the token at startup")
    dash.add_argument("--reports-dir", help="override [backtest].report_dir for report browsing")

    kill = sub.add_parser(
        "kill", help="engage or clear the file-based kill switch", parents=[_common_parent()]
    )
    kill.add_argument("--clear", action="store_true", help="clear the kill switch instead")
    kill.add_argument("--reason", default="manual", help="reason recorded in the KILL file")

    sub.add_parser("status", help="show config, kill switch, heartbeat and last run",
                   parents=[_common_parent()])
    sub.add_parser("doctor", help="run Raspberry Pi / broker pre-flight checks",
                   parents=[_common_parent()])
    sub.add_parser("strategies", help="list registered strategies and their parameters",
                   parents=[_common_parent()])

    flatten = sub.add_parser(
        "flatten",
        help="cancel all orders and liquidate all positions (Alpaca)",
        parents=[_common_parent()],
    )
    flatten.add_argument("--yes", action="store_true", required=True, help="confirm the liquidation")
    return parser


def _load_config(args: argparse.Namespace) -> Config:
    path = getattr(args, "config", None)
    if path is None and Path("configs/example.toml").is_file():
        path = "configs/example.toml"
    cfg = Config.from_file(path) if path else Config()
    if getattr(args, "log_level", None):
        cfg.app.log_level = args.log_level
    if getattr(args, "json_logs", False):
        cfg.app.log_json = True
    return cfg


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _setup_logging(cfg: Config) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if cfg.app.log_json:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, cfg.app.log_level.upper(), logging.INFO))
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _strategy(cfg: Config, args: argparse.Namespace):
    load_builtin()
    name = getattr(args, "strategy", None) or cfg.strategy.name
    # Config params belong to the configured strategy. When the operator selects
    # a different one on the command line, only its own defaults + overrides apply.
    params = deep_merge(cfg.strategy.params, {}) if name == cfg.strategy.name else {}
    overrides = {}
    for item in getattr(args, "param", []) or []:
        if "=" not in item:
            raise SystemExit(f"--param expects KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        overrides[key.strip()] = value.strip()
    params = deep_merge(params, coerce_params(name, overrides))
    return build(name, params)


def _fill_model(cfg: Config) -> FillModel:
    e = cfg.execution
    return FillModel(
        slippage_bps=e.slippage_bps,
        half_spread_bps=e.half_spread_bps,
        commission_per_share=e.commission_per_share,
        min_commission=e.min_commission,
        max_participation=e.max_participation,
        allow_partial_fills=e.allow_partial_fills,
        spread_bps_by_symbol=e.spread_costs(),
    )


def _alpaca_clients(cfg: Config) -> tuple[AlpacaClient, AlpacaClient]:
    key, secret = cfg.credentials()
    trading = AlpacaClient(trading_base_url(cfg.broker.paper), key, secret)
    data_url = cfg.broker.data_url or "https://data.alpaca.markets"
    data = AlpacaClient(data_url, key, secret)
    return trading, data


def _store(cfg: Config):
    return SqliteStore(cfg.storage.path) if cfg.storage.enabled else None


def _new_run_id(mode: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{mode}-{stamp}-{uuid.uuid4().hex[:4]}"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _prepare_feed(cfg: Config, args: argparse.Namespace):
    """Build the historical feed shared by backtest and optimize."""
    days = args.days or cfg.feed.synthetic_days
    if args.csv:
        feed = CsvFeed(args.csv, cfg.market.timeframe)
        if not args.symbols:
            # A CSV is self-describing: use every symbol it contains unless the
            # operator narrows it with --symbols.
            cfg.market.symbols = feed.symbols()
        return feed, None, None
    if cfg.feed.provider == "alpaca":
        _, data_client = _alpaca_clients(cfg)
        feed = AlpacaFeed(data_client, feed=cfg.feed.alpaca_feed)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        return feed, start, end
    if cfg.feed.provider == "csv":
        # Config symbols are authoritative for a configured CSV. Only `--csv`
        # auto-detects the universe, so an explicit pair does not silently grow.
        feed = CsvFeed(cfg.feed.csv_path, cfg.market.timeframe)
        return feed, None, None
    feed = SyntheticFeed(
        days=days,
        seed=args.seed if args.seed is not None else cfg.feed.synthetic_seed,
        volatility=cfg.feed.synthetic_volatility,
        drift=cfg.feed.synthetic_drift,
    )
    return feed, None, None


def cmd_backtest(cfg: Config, args: argparse.Namespace) -> int:
    cfg.app.mode = "backtest"
    if args.timeframe:
        cfg.market.timeframe = args.timeframe
    if args.symbols:
        cfg.market.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    cfg.ensure_dirs()
    strategy = _strategy(cfg, args)
    feed, start, end = _prepare_feed(cfg, args)

    store = SqliteStore(cfg.storage.path) if (args.store or cfg.storage.enabled) else None
    try:
        engine = BacktestEngine(cfg, strategy, feed, store=store)
        result = engine.run(start=start, end=end)

        from .report import render_summary, write_report

        if not args.quiet:
            print(render_summary(result))
        report_dir = args.report_dir or cfg.backtest.report_dir
        path = write_report(result, report_dir)
        print(f"\nreport: {path.resolve()}")
    finally:
        if store is not None:
            store.close()
    return 0


def cmd_optimize(cfg: Config, args: argparse.Namespace) -> int:
    cfg.app.mode = "backtest"
    if args.timeframe:
        cfg.market.timeframe = args.timeframe
    if args.symbols:
        cfg.market.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    cfg.ensure_dirs()
    load_builtin()
    name = args.strategy or cfg.strategy.name
    try:
        strategy_cls = get_strategy(name)
    except KeyError as exc:
        print(f"strategy error: {exc}", file=sys.stderr)
        return 2

    defaults = flatten(strategy_cls.defaults)
    spaces = []
    for raw in args.space:
        if "=" not in raw:
            raise SystemExit(f"--space expects NAME=SPEC, got {raw!r}")
        key, spec = raw.split("=", 1)
        key = key.strip()
        if key not in defaults:
            raise SystemExit(
                f"unknown parameter {key!r} for {name}; "
                f"available: {', '.join(sorted(defaults))}"
            )
        spaces.append(parse_space(key, spec.strip(), defaults[key]))

    overrides = {}
    for item in args.param or []:
        if "=" not in item:
            raise SystemExit(f"--param expects KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        overrides[key.strip()] = value.strip()
    base = deep_merge(cfg.strategy.params, {}) if name == cfg.strategy.name else {}
    base = deep_merge(base, coerce_params(name, overrides))

    feed, start, end = _prepare_feed(cfg, args)
    bars = load_bars(feed, cfg.market.symbols, cfg.market.timeframe, start, end)
    if not bars:
        raise SystemExit("no bars loaded; check --csv/--symbols/--days")

    optimizer = StrategyOptimizer(
        cfg,
        name,
        InMemoryFeed(bars),
        spaces,
        base_params=base,
        folds=args.folds,
        holdout=args.holdout,
        objective=args.objective,
        min_trades=args.min_trades,
        penalty=args.penalty,
        dd_penalty=args.dd_penalty,
        turnover_penalty=args.turnover_penalty,
        seed=args.seed,
    )
    print(
        f"optimizing {name} over {len(bars):,} bars | {len(spaces)} parameters | "
        f"method={args.method} trials={args.trials} folds={args.folds} "
        f"holdout={args.holdout:.0%}"
    )
    result = optimizer.run(args.method, args.trials)
    print(render_optimization_summary(result, top=args.top))
    report_dir = args.report_dir or cfg.backtest.report_dir
    path = write_optimization_report(result, report_dir)
    print(f"\nreport: {path.resolve()}")
    if result.best:
        print(f"best params: {result.best.params}")
    return 0


def cmd_runtime(cfg: Config, args: argparse.Namespace, mode: str) -> int:
    if mode == "live" and not getattr(args, "i_understand_the_risk", False):
        print(
            "refusing to trade real money without --i-understand-the-risk\n"
            "start with `krabs paper` or `krabs broker-paper` first.",
            file=sys.stderr,
        )
        return 2
    if mode == "live" and cfg.broker.paper:
        raise SystemExit("config error: [broker].paper must be false for live trading")
    if mode == "broker-paper" and not cfg.broker.paper:
        raise SystemExit("config error: [broker].paper must be true for broker-paper")
    offline = mode == "paper" and (
        bool(getattr(args, "offline", False)) or cfg.feed.offline
    )
    if not offline and cfg.feed.provider != "alpaca":
        raise SystemExit(
            f"mode {mode!r} needs live market data: set [feed].provider = 'alpaca' "
            "(use `krabs paper --offline` for a keyless deployment test)"
        )
    if mode == "live" and not cfg.storage.enabled:
        raise SystemExit("config error: live trading requires [storage].enabled = true")

    cfg.app.mode = mode
    cfg.ensure_dirs()
    strategy = _strategy(cfg, args)
    mode_label = "paper-sim" if offline else mode
    run_id = _new_run_id(mode_label)
    kill = KillSwitch(cfg.data_path("KILL"))
    if mode == "live" and kill.engaged:
        raise SystemExit(f"kill switch is engaged: {kill.reason()} -- clear it first")

    if offline:
        cfg.market.enforce_market_hours = False
        feed = SyntheticLiveFeed(
            cfg.market.symbols,
            seed=cfg.feed.synthetic_seed,
            history_days=cfg.market.history_days,
            volatility=cfg.feed.synthetic_volatility,
            drift=cfg.feed.synthetic_drift,
        )
    else:
        trading_client, data_client = _alpaca_clients(cfg)
        feed = AlpacaFeed(data_client, feed=cfg.feed.alpaca_feed)

    if mode == "paper":
        wallet = PaperWallet(
            cfg.wallet.starting_cash,
            currency=cfg.wallet.currency,
            allow_short=cfg.wallet.allow_short,
            margin_multiplier=cfg.wallet.margin_multiplier,
        )
        risk = RiskEngine(
            cfg.risk, wallet, symbols=cfg.market.symbols, kill=kill,
            logger=logging.getLogger("krabs.risk"),
        )
        broker = PaperBroker(
            wallet,
            RealClock(),
            _fill_model(cfg),
            run_id=run_id,
            logger=logging.getLogger("krabs.broker.paper"),
        )
    else:
        wallet = BrokerWallet(
            cfg.wallet.starting_cash,
            currency=cfg.wallet.currency,
            allow_short=cfg.wallet.allow_short,
            margin_multiplier=cfg.wallet.margin_multiplier,
        )
        risk = RiskEngine(
            cfg.risk, wallet, symbols=cfg.market.symbols, kill=kill,
            logger=logging.getLogger("krabs.risk"),
        )
        broker = AlpacaBroker(
            trading_client,
            run_id=run_id,
            price_fn=lambda symbol: risk.prices().get(symbol, 0.0),
        )

    store = _store(cfg)
    runtime = TradingRuntime(
        cfg,
        strategy,
        feed,
        broker,
        wallet,
        risk,
        run_id=run_id,
        mode=mode_label,
        kill=kill,
        store=store,
    )
    print(
        f"Krabs {mode_label} | run {run_id} | {','.join(cfg.market.symbols)} "
        f"{cfg.market.timeframe} | {strategy.id} {strategy.params} | kill={kill.path}"
    )
    if offline:
        print(
            "OFFLINE SIMULATION: accelerated synthetic bars. No broker connection, "
            "no real or paper orders."
        )
    if mode == "live":
        print("LIVE TRADING: real orders will be sent. Ctrl-C stops gracefully.")
    try:
        stats = asyncio.run(
            runtime.run(
                max_cycles=args.max_cycles or None,
                flatten_on_exit=args.flatten_on_exit,
            )
        )
    except KeyboardInterrupt:
        stats = runtime.stats
    except BrokerError as exc:
        print(f"broker error during startup: {exc}", file=sys.stderr)
        return 3
    finally:
        if store is not None:
            store.close()
    print(
        f"stopped: cycles={stats.cycles} bars={stats.bars} orders={stats.orders} "
        f"fills={stats.fills} errors={stats.errors}"
    )
    return 0


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _dashboard_token(cfg: Config, args: argparse.Namespace) -> tuple[str, Path | None]:
    if args.no_auth:
        return "", None
    if args.token:
        return args.token, None
    path = Path(args.token_file) if args.token_file else cfg.data_path("dashboard.token")
    try:
        if path.is_file():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token, path
        token = secrets.token_urlsafe(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token + "\n", encoding="utf-8")
        path.chmod(0o600)
    except OSError as exc:
        raise SystemExit(f"cannot read/write dashboard token {path}: {exc}") from exc
    return token, path


def cmd_dashboard(cfg: Config, args: argparse.Namespace) -> int:
    if args.no_auth and not _is_loopback(args.host):
        print("refusing --no-auth on a non-loopback host; use the token", file=sys.stderr)
        return 2
    if args.port < 1 or args.port > 65535:
        print("port must be between 1 and 65535", file=sys.stderr)
        return 2
    cfg.ensure_dirs()
    token, token_path = _dashboard_token(cfg, args)
    reports_dir = args.reports_dir or cfg.backtest.report_dir
    data = DashboardData(cfg, reports_dir=reports_dir)

    print(f"Krabs dashboard {__version__}")
    print(f"  url         : http://{args.host}:{args.port}/")
    if token_path is not None:
        print(f"  token file  : {token_path.resolve()}")
    print(
        "  controls    : "
        + ("kill switch enabled" if args.allow_controls else "read-only")
    )
    if not _is_loopback(args.host):
        print("  WARNING     : bound to a non-loopback interface; use TLS (Tailscale/Caddy)")
    if args.no_auth:
        print("  WARNING     : authentication disabled (loopback only)")
    if args.show_token:
        print(f"  token       : {token}")
    print("  mobile      : use Tailscale Serve or an SSH tunnel; see docs/dashboard.md")
    try:
        serve_dashboard(
            data,
            host=args.host,
            port=args.port,
            token=token,
            no_auth=args.no_auth,
            allow_controls=args.allow_controls,
        )
    except OSError as exc:
        print(f"dashboard failed to start: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_kill(cfg: Config, args: argparse.Namespace) -> int:
    kill = KillSwitch(cfg.data_path("KILL"))
    if args.clear:
        kill.clear()
        print(f"kill switch cleared ({kill.path})")
    else:
        kill.engage(args.reason)
        print(f"kill switch ENGAGED ({kill.path}): {args.reason}")
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    print(f"Krabs {__version__}")
    print(f"  config mode    : {cfg.app.mode}")
    print(f"  symbols        : {', '.join(cfg.market.symbols)} ({cfg.market.timeframe})")
    print(f"  strategy       : {cfg.strategy.name} {cfg.strategy.params}")
    feed_label = cfg.feed.provider + (" (offline)" if cfg.feed.offline else "")
    print(f"  feed / broker  : {feed_label} / {cfg.broker.provider} "
          f"(paper={cfg.broker.paper})")
    print(f"  data dir       : {Path(cfg.app.data_dir).resolve()}")

    kill = KillSwitch(cfg.data_path("KILL"))
    print(f"  kill switch    : {'ENGAGED - ' + kill.reason() if kill.engaged else 'clear'}")

    heartbeat = Path(cfg.app.data_dir) / "heartbeat.json"
    if heartbeat.exists():
        try:
            payload = json.loads(heartbeat.read_text(encoding="utf-8"))
            ts = datetime.fromisoformat(payload["ts"])
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            print(f"  heartbeat      : {age:.0f}s ago, mode={payload.get('mode')} "
                  f"equity={payload.get('equity')} halted={payload.get('halted')}")
            print(f"  positions      : {payload.get('positions') or 'none'}")
        except (OSError, ValueError, KeyError) as exc:
            print(f"  heartbeat      : unreadable ({exc})")
    else:
        print("  heartbeat      : none")

    if cfg.storage.enabled and Path(cfg.storage.path).exists():
        with SqliteStore(cfg.storage.path) as store:
            last = store.last_run()
            if last:
                print(f"  last run       : {last['run_id']} {last['strategy']} "
                      f"{last['status']} equity={last['final_equity']}")
                print(f"  audit counts   : {store.counts(last['run_id'])}")
            else:
                print("  last run       : none recorded")
    elif cfg.storage.enabled:
        print("  audit trail    : enabled (no database yet)")
    else:
        print("  audit trail    : disabled")
    return 0


def cmd_flatten(cfg: Config, args: argparse.Namespace) -> int:
    key, secret = cfg.credentials()
    client = AlpacaClient(trading_base_url(cfg.broker.paper), key, secret)
    account = client.request("GET", "/v2/account")
    print(
        f"liquidating equity={account.get('equity')} "
        f"buying_power={account.get('buying_power')} on "
        f"{'paper' if cfg.broker.paper else 'LIVE'} account"
    )
    client.request("DELETE", "/v2/positions", params={"cancel_orders": "true"})
    print("all orders cancelled and positions closed")
    return 0


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    return print_checks(run_checks(cfg))


def cmd_strategies(cfg: Config, args: argparse.Namespace) -> int:
    load_builtin()
    for info in describe():
        params = " ".join(f"{k}={v}" for k, v in info.defaults.items())
        print(f"{info.id:<16} {params}")
        print(f"{'':<16} {info.description}")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        cfg = _load_config(args)
    except (ConfigError, FileNotFoundError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    _setup_logging(cfg)

    handlers = {
        "backtest": lambda: cmd_backtest(cfg, args),
        "optimize": lambda: cmd_optimize(cfg, args),
        "paper": lambda: cmd_runtime(cfg, args, "paper"),
        "broker-paper": lambda: cmd_runtime(cfg, args, "broker-paper"),
        "live": lambda: cmd_runtime(cfg, args, "live"),
        "dashboard": lambda: cmd_dashboard(cfg, args),
        "kill": lambda: cmd_kill(cfg, args),
        "status": lambda: cmd_status(cfg, args),
        "flatten": lambda: cmd_flatten(cfg, args),
        "doctor": lambda: cmd_doctor(cfg, args),
        "strategies": lambda: cmd_strategies(cfg, args),
    }
    try:
        return handlers[args.command]()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except (KeyError, ValueError) as exc:
        print(f"strategy error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"file error: {exc}", file=sys.stderr)
        return 2
    except BrokerError as exc:
        print(f"broker error: {exc}", file=sys.stderr)
        return 3
    except RuntimeError as exc:
        print(f"optimization error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
