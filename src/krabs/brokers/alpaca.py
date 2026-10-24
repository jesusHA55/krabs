"""Minimal Alpaca REST client (stdlib only).

Uses ``urllib`` on purpose: the trading service must run on a stock Raspberry Pi
OS image with nothing but Python installed. WebSocket streaming is an optional
upgrade (``pip install krabs[alpaca]``); REST polling is the supported default.

Endpoints used:
  * trading:  https://paper-api.alpaca.markets  |  https://api.alpaca.markets
  * data:     https://data.alpaca.markets
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..events import (
    AccountState,
    Order,
    OrderIntent,
    OrderStatus,
    OrderType,
    Position,
    Side,
    TimeInForce,
    ensure_utc,
)
from .base import BrokerError, client_order_id

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"

_RETRYABLE = {429, 500, 502, 503, 504}


class BrokerRejected(BrokerError):
    """HTTP 4xx from the broker: the request itself was invalid."""


class AlpacaClient:
    def __init__(
        self,
        base_url: str,
        key: str,
        secret: str,
        *,
        timeout: float = 15.0,
        retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.secret = secret
        self.timeout = timeout
        self.retries = retries

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}?{urlencode(clean, doseq=True)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "APCA-API-KEY-ID": self.key,
            "APCA-API-SECRET-KEY": self.secret,
            "Accept": "application/json",
            "User-Agent": "krabs/0.1",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            request = Request(url, data=data, headers=headers, method=method)
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    raw = response.read()
                    return json.loads(raw) if raw else None
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:500]
                if exc.code in _RETRYABLE and attempt < self.retries:
                    last_error = exc
                    time.sleep(0.5 * (2**attempt))
                    continue
                if 400 <= exc.code < 500:
                    raise BrokerRejected(
                        f"{method} {path} -> HTTP {exc.code}: {detail}"
                    ) from exc
                raise BrokerError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise BrokerError(f"{method} {path} failed: {exc}") from exc
        raise BrokerError(f"{method} {path} failed after retries: {last_error}")


def trading_base_url(paper: bool) -> str:
    return PAPER_URL if paper else LIVE_URL


class AlpacaBroker:
    """Execution adapter. The broker is the source of truth for account state."""

    def __init__(
        self,
        client: AlpacaClient,
        *,
        run_id: str,
        price_fn,
        seq_start: int = 0,
    ) -> None:
        self.client = client
        self.run_id = run_id
        self.price_fn = price_fn
        self._seq = seq_start
        self._cache: dict[str, Order] = {}

    # -- execution -------------------------------------------------------
    def submit(self, intent: OrderIntent) -> Order:
        price = self.price_fn(intent.symbol)
        qty = intent.qty
        if qty is None:
            if not price:
                raise BrokerError(f"no reference price to size notional for {intent.symbol}")
            qty = float(intent.notional or 0.0) / price
        if qty <= 0:
            raise BrokerError("resolved quantity is zero")

        self._seq += 1
        cid = client_order_id(self.run_id, self._seq)
        body: dict[str, Any] = {
            "symbol": intent.symbol,
            "qty": _num(qty),
            "side": intent.side.value,
            "type": intent.type.value,
            "time_in_force": intent.tif.value,
            "client_order_id": cid,
        }
        if intent.limit_price:
            body["limit_price"] = _num(intent.limit_price)
        if intent.stop_price:
            body["stop_price"] = _num(intent.stop_price)

        order = Order(client_id=cid, intent=intent, qty=qty)
        try:
            payload = self.client.request("POST", "/v2/orders", body=body)
        except BrokerRejected as exc:
            # A retry after a lost response can hit "client_order_id must be
            # unique". The order exists: adopt it instead of declaring failure.
            if "client_order_id" in str(exc) and "unique" in str(exc).lower():
                existing = self.get_order(cid)
                if existing is not None:
                    return existing
            order.status = OrderStatus.REJECTED
            order.reject_reason = str(exc)
            self._cache[cid] = order
            return order
        mapped = self._parse(payload, intent, qty)
        self._cache[cid] = mapped
        return mapped

    def cancel(self, client_id: str) -> bool:
        cached = self._cache.get(client_id)
        broker_id = cached.broker_id if cached else ""
        if not broker_id:
            order = self.get_order(client_id)
            broker_id = order.broker_id if order else client_id
        try:
            self.client.request("DELETE", f"/v2/orders/{broker_id}")
            return True
        except BrokerRejected:
            return False

    def cancel_all(self) -> bool:
        try:
            self.client.request("DELETE", "/v2/orders")
            return True
        except BrokerRejected:
            return False

    def close_all(self) -> bool:
        """Cancel working orders and liquidate every position (operator action)."""
        try:
            self.client.request("DELETE", "/v2/positions", params={"cancel_orders": "true"})
            return True
        except BrokerRejected:
            return False

    def get_order(self, client_id: str) -> Order | None:
        cached = self._cache.get(client_id)
        try:
            payload = self.client.request(
                "GET",
                "/v2/orders:by_client_order_id",
                params={"client_order_id": client_id},
            )
        except BrokerRejected:
            return cached
        if not payload:
            return cached
        intent = cached.intent if cached else OrderIntent(
            symbol=payload["symbol"],
            side=Side(payload["side"]),
            qty=float(payload.get("qty") or 1.0),
        )
        order = self._parse(payload, intent, cached.qty if cached else float(payload["qty"]))
        self._cache[client_id] = order
        return order

    def open_orders(self) -> list[Order]:
        payload = self.client.request(
            "GET", "/v2/orders", params={"status": "open", "limit": 500, "direction": "asc"}
        )
        orders = [self._parse(item) for item in payload or []]
        for order in orders:
            self._cache[order.client_id] = order
        return orders

    def recent_orders(self, limit: int = 500) -> list[Order]:
        payload = self.client.request(
            "GET", "/v2/orders", params={"status": "all", "limit": limit, "direction": "desc"}
        )
        orders = [self._parse(item) for item in payload or []]
        for order in orders:
            self._cache[order.client_id] = order
        return orders

    def account(self) -> AccountState:
        payload = self.client.request("GET", "/v2/account")
        return AccountState(
            ts=datetime.now(timezone.utc),
            cash=float(payload.get("cash", 0.0)),
            equity=float(payload.get("equity", 0.0)),
            buying_power=float(payload.get("buying_power", 0.0)),
            long_value=float(payload.get("long_market_value", 0.0)),
            short_value=float(payload.get("short_market_value", 0.0)),
            realized_pnl=0.0,
            unrealized_pnl=float(payload.get("unrealized_pl", 0.0) or 0.0),
            day_start_equity=float(payload.get("last_equity", payload.get("equity", 0.0))),
            currency=payload.get("currency", "USD"),
        )

    def positions(self) -> dict[str, Position]:
        payload = self.client.request("GET", "/v2/positions")
        out: dict[str, Position] = {}
        for item in payload or []:
            symbol = item["symbol"]
            out[symbol] = Position(
                symbol=symbol,
                qty=float(item.get("qty", 0.0)),
                avg_price=float(item.get("avg_entry_price", 0.0)),
                market_price=float(item.get("current_price", 0.0) or 0.0),
            )
        return out

    def reconcile(self) -> list[str]:
        """Return human-readable mismatch descriptions (empty if clean)."""
        issues: list[str] = []
        positions = self.positions()
        for symbol, pos in positions.items():
            if pos.market_price <= 0:
                issues.append(f"{symbol}: broker reports no current price")
            if abs(pos.qty) < 1e-9:
                issues.append(f"{symbol}: broker returned a zero position")
        return issues

    # -- parsing ---------------------------------------------------------
    def _parse(
        self, payload: dict[str, Any], intent: OrderIntent | None = None, qty: float | None = None
    ) -> Order:
        cid = payload.get("client_order_id") or payload.get("id", "")
        if intent is None:
            intent = OrderIntent(
                symbol=payload["symbol"].upper(),
                side=Side(payload["side"]),
                qty=float(payload.get("qty") or 1.0),
                type=_order_type(payload.get("type", "market")),
                limit_price=_opt_float(payload.get("limit_price")),
                stop_price=_opt_float(payload.get("stop_price")),
                tif=_tif(payload.get("time_in_force", "day")),
            )
        submitted = payload.get("submitted_at") or payload.get("created_at")
        created = _parse_ts(submitted) if submitted else datetime.now(timezone.utc)
        updated = _parse_ts(payload.get("updated_at")) if payload.get("updated_at") else created
        return Order(
            client_id=cid,
            intent=intent,
            qty=qty or float(payload.get("qty") or 0.0),
            status=_status(payload.get("status", "new")),
            filled_qty=float(payload.get("filled_qty") or 0.0),
            avg_price=float(payload.get("filled_avg_price") or 0.0),
            created_at=created,
            updated_at=updated,
            broker_id=payload.get("id", ""),
            reject_reason=payload.get("reject_reason") or "",
        )


def _status(value: str) -> OrderStatus:
    return {
        "new": OrderStatus.NEW,
        "accepted": OrderStatus.NEW,
        "pending_new": OrderStatus.NEW,
        "partially_filled": OrderStatus.PARTIALLY_FILLED,
        "filled": OrderStatus.FILLED,
        "canceled": OrderStatus.CANCELED,
        "pending_cancel": OrderStatus.NEW,
        "expired": OrderStatus.EXPIRED,
        "rejected": OrderStatus.REJECTED,
        "replaced": OrderStatus.CANCELED,
        "pending_replace": OrderStatus.NEW,
        "held": OrderStatus.NEW,
        "suspended": OrderStatus.NEW,
        "calculated": OrderStatus.FILLED,
    }.get(value, OrderStatus.NEW)


def _order_type(value: str) -> OrderType:
    return {
        "market": OrderType.MARKET,
        "limit": OrderType.LIMIT,
        "stop": OrderType.STOP,
        "stop_limit": OrderType.STOP_LIMIT,
    }.get(value, OrderType.MARKET)


def _tif(value: str) -> TimeInForce:
    return {
        "day": TimeInForce.DAY,
        "gtc": TimeInForce.GTC,
        "ioc": TimeInForce.IOC,
        "fok": TimeInForce.FOK,
    }.get(value, TimeInForce.DAY)


def _num(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _opt_float(value: Any) -> float | None:
    return float(value) if value not in (None, "") else None


def _parse_ts(value: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
