"""Core value types.

Everything that crosses a component boundary is a frozen, timestamped value
object defined here. Times are always timezone-aware UTC; the exchange calendar
is the only place that converts to a market timezone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TypeAlias


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(ts: datetime) -> datetime:
    """Normalise a datetime to aware UTC. Naive input is assumed to be UTC."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class OrderStatus(str, Enum):
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def is_open(self) -> bool:
        return self in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)

    @property
    def is_terminal(self) -> bool:
        return not self.is_open


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bar:
    """OHLCV bar. ``ts`` is the *close* time of the bar, in UTC."""

    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: str = "1Min"

    def __post_init__(self) -> None:
        if self.symbol != self.symbol.upper():
            raise ValueError(f"symbol must be upper-case: {self.symbol!r}")
        if self.close <= 0 or self.open <= 0 or self.high <= 0 or self.low <= 0:
            raise ValueError(f"non-positive price in bar {self.symbol} {self.ts}")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError(f"inconsistent OHLC in bar {self.symbol} {self.ts}")
        if self.volume < 0:
            raise ValueError(f"negative volume in bar {self.symbol} {self.ts}")


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    ts: datetime
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    ts: datetime
    price: float
    size: float = 0.0


MarketEvent: TypeAlias = Bar | Quote | Trade


# ---------------------------------------------------------------------------
# Orders and fills
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """A strategy's *wish*. Risk may adjust or veto it before it becomes an Order."""

    symbol: str
    side: Side
    qty: float | None = None
    notional: float | None = None
    type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None
    tif: TimeInForce = TimeInForce.DAY
    strategy_id: str = "manual"
    reason: str = ""
    tag: str = ""

    def __post_init__(self) -> None:
        if self.symbol != self.symbol.upper():
            raise ValueError(f"symbol must be upper-case: {self.symbol!r}")
        if (self.qty is None) == (self.notional is None):
            raise ValueError("exactly one of qty or notional must be set")
        if self.qty is not None and self.qty <= 0:
            raise ValueError("qty must be positive")
        if self.notional is not None and self.notional <= 0:
            raise ValueError("notional must be positive")
        if self.notional is not None and self.type is not OrderType.MARKET:
            raise ValueError("notional orders must be market orders")
        if self.type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and not self.limit_price:
            raise ValueError(f"{self.type.value} order requires limit_price")
        if self.type in (OrderType.STOP, OrderType.STOP_LIMIT) and not self.stop_price:
            raise ValueError(f"{self.type.value} order requires stop_price")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit_price must be positive")
        if self.stop_price is not None and self.stop_price <= 0:
            raise ValueError("stop_price must be positive")

    @property
    def is_market(self) -> bool:
        return self.type is OrderType.MARKET


@dataclass(slots=True)
class Order:
    """An order accepted by an execution adapter.

    ``qty`` is always a share quantity: notional intents are converted at
    submission time using the last known reference price.
    """

    client_id: str
    intent: OrderIntent
    qty: float
    status: OrderStatus = OrderStatus.NEW
    filled_qty: float = 0.0
    avg_price: float = 0.0
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    broker_id: str = ""
    reject_reason: str = ""
    commission: float = 0.0
    triggered: bool = False

    @property
    def remaining(self) -> float:
        return max(0.0, self.qty - self.filled_qty)

    @property
    def is_open(self) -> bool:
        return self.status.is_open

    @property
    def symbol(self) -> str:
        return self.intent.symbol

    @property
    def side(self) -> Side:
        return self.intent.side


@dataclass(frozen=True, slots=True)
class Fill:
    order_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    ts: datetime
    commission: float = 0.0
    strategy_id: str = "manual"
    tag: str = ""

    @property
    def notional(self) -> float:
        return self.qty * self.price

    @property
    def cash_delta(self) -> float:
        """Signed effect on cash, including commission."""
        return -self.side.sign * self.notional - self.commission


# ---------------------------------------------------------------------------
# Portfolio state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Position:
    symbol: str
    qty: float = 0.0
    avg_price: float = 0.0
    market_price: float = 0.0

    @property
    def price(self) -> float:
        return self.market_price or self.avg_price

    @property
    def market_value(self) -> float:
        return self.qty * self.price

    @property
    def unrealized_pnl(self) -> float:
        return (self.price - self.avg_price) * self.qty

    @property
    def is_flat(self) -> bool:
        return abs(self.qty) < 1e-9


@dataclass(frozen=True, slots=True)
class AccountState:
    ts: datetime
    cash: float
    equity: float
    buying_power: float
    long_value: float = 0.0
    short_value: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    day_start_equity: float = 0.0
    currency: str = "USD"

    @property
    def gross_exposure(self) -> float:
        return self.long_value + abs(self.short_value)

    @property
    def day_pnl(self) -> float:
        return self.equity - self.day_start_equity

    @property
    def day_return(self) -> float:
        if not self.day_start_equity:
            return 0.0
        return self.day_pnl / self.day_start_equity
