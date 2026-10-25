"""Typed, validated configuration loaded from TOML.

Rules:
  * Unknown keys are an error (typos in a live trading config are dangerous).
  * Every value has a default so a minimal config file is valid.
  * Secrets never live in TOML. They are read from environment variables named
    in ``[broker]``, optionally from a systemd ``LoadCredential`` style file.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

from .spreads import SpreadTable, load_spread_table

MODES = ("backtest", "paper", "broker-paper", "live")
TIMEFRAMES = ("1Min", "5Min", "15Min", "30Min", "1Hour", "1Day")


class ConfigError(ValueError):
    pass


@dataclass(slots=True)
class AppConfig:
    name: str = "krabs"
    mode: str = "backtest"
    data_dir: str = "var"
    log_level: str = "INFO"
    log_json: bool = False


@dataclass(slots=True)
class MarketConfig:
    symbols: list[str] = field(default_factory=lambda: ["SPY"])
    timeframe: str = "1Min"
    history_days: int = 5
    enforce_market_hours: bool = True


@dataclass(slots=True)
class WalletConfig:
    starting_cash: float = 100_000.0
    currency: str = "USD"
    allow_short: bool = False
    margin_multiplier: float = 1.0
    fractional: bool = True


@dataclass(slots=True)
class ExecutionConfig:
    slippage_bps: float = 1.0
    half_spread_bps: float = 1.5
    spread_bps: dict[str, float] = field(default_factory=dict)
    spreads_csv: str = ""
    spread_multiplier: float = 1.0
    borrow_bps_per_year: float = 0.0
    borrow_bps: dict[str, float] = field(default_factory=dict)
    borrow_csv: str = ""
    delay_bars: int = 0
    commission_per_share: float = 0.0
    min_commission: float = 0.0
    max_participation: float = 0.25
    impact_coefficient_bps: float = 0.0
    allow_partial_fills: bool = True

    def spread_table(self) -> SpreadTable:
        """Static or as-of spread table with inline values as the base."""
        base = {str(k).upper(): float(v) for k, v in self.spread_bps.items()}
        if self.spreads_csv:
            return load_spread_table(
                self.spreads_csv,
                multiplier=self.spread_multiplier,
                base=base,
            )
        return SpreadTable(
            static=base,
            multiplier=self.spread_multiplier,
            source="params" if base else "default",
        )

    def spread_costs(self) -> dict[str, float]:
        """Latest full quoted spread per symbol."""
        return self.spread_table().latest()

    def borrow_rates(self) -> dict[str, float]:
        """Annual short-borrow bps per symbol; global rate is the fallback."""
        rates = {str(k).upper(): float(v) for k, v in self.borrow_bps.items()}
        if self.borrow_csv:
            from .borrow import load_borrow_table

            rates.update(
                {
                    symbol: item.bps_per_year
                    for symbol, item in load_borrow_table(
                        self.borrow_csv, base=rates
                    ).items()
                }
            )
        return rates


@dataclass(slots=True)
class RiskConfig:
    max_order_notional: float = 25_000.0
    max_position_pct: float = 0.25
    max_gross_exposure_pct: float = 0.95
    max_daily_loss_pct: float = 0.03
    max_open_orders: int = 20
    max_data_age_s: float = 120.0
    price_collar_pct: float = 0.05
    duplicate_window_s: float = 30.0
    allowed_symbols: list[str] = field(default_factory=list)


@dataclass(slots=True)
class BrokerConfig:
    provider: str = "alpaca"
    paper: bool = True
    base_url: str = ""
    data_url: str = ""
    api_key_env: str = "ALPACA_API_KEY"
    api_secret_env: str = "ALPACA_SECRET_KEY"


@dataclass(slots=True)
class FeedConfig:
    provider: str = "synthetic"
    csv_path: str = ""
    poll_seconds: float = 5.0
    warmup_bars: int = 300
    alpaca_feed: str = "iex"
    offline: bool = False
    synthetic_days: int = 90
    synthetic_seed: int = 7
    synthetic_volatility: float = 0.22
    synthetic_drift: float = 0.08


@dataclass(slots=True)
class StorageConfig:
    enabled: bool = False
    path: str = "var/krabs.db"


@dataclass(slots=True)
class BacktestConfig:
    report_dir: str = "reports"


@dataclass(slots=True)
class StrategyConfig:
    name: str = "sma_cross"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Config:
    app: AppConfig = field(default_factory=AppConfig)
    market: MarketConfig = field(default_factory=MarketConfig)
    wallet: WalletConfig = field(default_factory=WalletConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    feed: FeedConfig = field(default_factory=FeedConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)

    # -- loading ---------------------------------------------------------
    @classmethod
    def from_file(cls, path: str | Path) -> Config:
        p = Path(path)
        if not p.is_file():
            raise ConfigError(f"config file not found: {p}")
        try:
            raw = tomllib.loads(p.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {p}: {exc}") from exc
        cfg = _build(cls, raw, "config")
        cfg.validate()
        return cfg

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Config:
        cfg = _build(cls, raw, "config")
        cfg.validate()
        return cfg

    # -- validation ------------------------------------------------------
    def validate(self) -> None:
        if self.app.mode not in MODES:
            raise ConfigError(f"app.mode must be one of {MODES}, got {self.app.mode!r}")
        if not self.market.symbols:
            raise ConfigError("market.symbols must not be empty")
        self.market.symbols = [s.strip().upper() for s in self.market.symbols]
        if self.market.timeframe not in TIMEFRAMES:
            raise ConfigError(f"market.timeframe must be one of {TIMEFRAMES}")
        if self.wallet.starting_cash <= 0:
            raise ConfigError("wallet.starting_cash must be positive")
        if self.wallet.margin_multiplier < 1.0:
            raise ConfigError("wallet.margin_multiplier must be >= 1.0")
        if self.market.history_days <= 0:
            raise ConfigError("market.history_days must be positive")
        if self.feed.provider not in ("synthetic", "csv", "alpaca"):
            raise ConfigError("feed.provider must be synthetic, csv or alpaca")
        if self.feed.provider == "csv" and not self.feed.csv_path:
            raise ConfigError("feed.csv_path is required when feed.provider = 'csv'")
        if self.feed.offline and self.feed.provider != "synthetic":
            raise ConfigError("feed.offline requires feed.provider = 'synthetic'")
        if self.broker.provider not in ("alpaca", "paper"):
            raise ConfigError("broker.provider must be 'alpaca' or 'paper'")
        for name in (
            "max_order_notional",
            "max_position_pct",
            "max_gross_exposure_pct",
            "max_daily_loss_pct",
            "price_collar_pct",
        ):
            if getattr(self.risk, name) <= 0:
                raise ConfigError(f"risk.{name} must be positive")
        if not 0 < self.risk.max_daily_loss_pct <= 1:
            raise ConfigError("risk.max_daily_loss_pct must be in (0, 1]")
        if not 0 < self.risk.max_gross_exposure_pct <= 10:
            raise ConfigError("risk.max_gross_exposure_pct must be in (0, 10]")
        if self.execution.half_spread_bps < 0 or self.execution.slippage_bps < 0:
            raise ConfigError("execution spread/slippage costs must be >= 0")
        if self.execution.spread_multiplier <= 0:
            raise ConfigError("execution.spread_multiplier must be positive")
        if self.execution.borrow_bps_per_year < 0:
            raise ConfigError("execution.borrow_bps_per_year must be >= 0")
        if self.execution.delay_bars < 0:
            raise ConfigError("execution.delay_bars must be >= 0")
        if self.execution.impact_coefficient_bps < 0:
            raise ConfigError("execution.impact_coefficient_bps must be >= 0")
        for symbol, bps in self.execution.spread_bps.items():
            if float(bps) < 0:
                raise ConfigError(f"execution.spread_bps.{symbol} must be >= 0")

    # -- helpers ---------------------------------------------------------
    def data_path(self, *parts: str) -> Path:
        return Path(self.app.data_dir).joinpath(*parts)

    def ensure_dirs(self) -> None:
        self.data_path().mkdir(parents=True, exist_ok=True)
        if self.storage.enabled:
            Path(self.storage.path).parent.mkdir(parents=True, exist_ok=True)
        if self.app.mode == "backtest":
            Path(self.backtest.report_dir).mkdir(parents=True, exist_ok=True)

    def credentials(self) -> tuple[str, str]:
        key = os.environ.get(self.broker.api_key_env, "")
        secret = os.environ.get(self.broker.api_secret_env, "")
        if not key or not secret:
            raise ConfigError(
                f"missing broker credentials: set {self.broker.api_key_env} and "
                f"{self.broker.api_secret_env} (see deploy/krabs.env.example)"
            )
        return key, secret

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Generic dataclass builder with strict unknown-key detection
# ---------------------------------------------------------------------------


def _build(cls: type, data: Any, where: str) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"{where} must be a table, got {type(data).__name__}")
    hints = get_type_hints(cls)
    names = {f.name for f in fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ConfigError(f"unknown key(s) in [{where}]: {', '.join(sorted(unknown))}")
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        path = f"{where}.{f.name}" if where != "config" else f.name
        kwargs[f.name] = _convert(hints[f.name], data[f.name], path)
    return cls(**kwargs)


def _convert(tp: Any, raw: Any, where: str) -> Any:
    origin = get_origin(tp)
    if is_dataclass(tp):
        return _build(tp, raw, where)
    if origin is list:
        if not isinstance(raw, list):
            raise ConfigError(f"{where} must be an array")
        (inner,) = get_args(tp) or (Any,)
        return [_convert(inner, item, f"{where}[]") for item in raw]
    if origin is dict:
        if not isinstance(raw, dict):
            raise ConfigError(f"{where} must be a table")
        args = get_args(tp)
        key_t, val_t = args if len(args) == 2 else (str, Any)
        return {
            _convert(key_t, k, f"{where}.key"): _convert(val_t, v, f"{where}.{k}")
            for k, v in raw.items()
        }
    if tp is bool:
        if not isinstance(raw, bool):
            raise ConfigError(f"{where} must be true or false")
        return raw
    if tp is float:
        # TOML has no float/int distinction requirement for our purposes.
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ConfigError(f"{where} must be a number")
        return float(raw)
    if tp is int:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or int(raw) != raw:
            raise ConfigError(f"{where} must be an integer")
        return int(raw)
    if tp is str:
        if not isinstance(raw, str):
            raise ConfigError(f"{where} must be a string")
        return raw
    return raw
