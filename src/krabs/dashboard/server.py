"""Dependency-free HTTP dashboard for Krabs.

Read-only by default. The only mutation is the file-based kill switch, and only
when the server is started with ``--allow-controls``. Run it as a separate
process from the trading bot so a dashboard bug cannot affect execution.

Security model:
  * binds to 127.0.0.1 by default;
  * a random token is generated on first run (``var/dashboard.token``);
  * login creates a session cookie + a double-submit CSRF cookie;
  * constant-time token comparison, login rate limiting, strict CSP;
  * no CORS headers, no directory traversal, no secrets in responses.
Put it behind Tailscale Serve/Caddy (HTTPS) if you need remote access.
"""

from __future__ import annotations

import json
import logging
import math
import secrets
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from .data import DashboardData

LOG = logging.getLogger("krabs.dashboard")
STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "krabs_session"
CSRF_COOKIE = "krabs_csrf"
MAX_BODY = 64 * 1024
SESSION_TTL = 7 * 24 * 3600


class SessionStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[str, float]] = {}

    def create(self) -> tuple[str, str]:
        session = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._prune(now)
            if len(self._sessions) > 100:
                self._sessions.pop(next(iter(self._sessions)), None)
            self._sessions[session] = (csrf, now + SESSION_TTL)
        return session, csrf

    def csrf(self, session: str) -> str | None:
        now = time.time()
        with self._lock:
            self._prune(now)
            entry = self._sessions.get(session)
            return entry[0] if entry else None

    def drop(self, session: str) -> None:
        with self._lock:
            self._sessions.pop(session, None)

    def _prune(self, now: float) -> None:
        expired = [key for key, (_, expires) in self._sessions.items() if expires < now]
        for key in expired:
            self._sessions.pop(key, None)


class DashboardAuth:
    def __init__(self, token: str, *, no_auth: bool = False) -> None:
        self.token = token
        self.no_auth = no_auth
        self.sessions = SessionStore()

    def token_ok(self, value: str | None) -> bool:
        if not value or not self.token:
            return False
        return secrets.compare_digest(value, self.token)

    def authorize(
        self, handler: BaseHTTPRequestHandler, *, cookie_only: bool = False
    ) -> tuple[bool, str | None, str]:
        """Return (authorized, csrf_for_this_session, auth_kind)."""
        if self.no_auth:
            return True, None, "none"
        if not cookie_only:
            header = handler.headers.get("Authorization", "")
            if header.lower().startswith("bearer "):
                if self.token_ok(header[7:].strip()):
                    return True, None, "bearer"
        cookie = SimpleCookie()
        try:
            cookie.load(handler.headers.get("Cookie", ""))
        except Exception:  # noqa: BLE001 - malformed cookie header
            return False, None, ""
        morsel = cookie.get(SESSION_COOKIE)
        if morsel:
            csrf = self.sessions.csrf(morsel.value)
            if csrf:
                return True, csrf, "cookie"
        return False, None, ""

    def create_session(self) -> tuple[str, str]:
        return self.sessions.create()

    def logout(self, handler: BaseHTTPRequestHandler) -> None:
        cookie = SimpleCookie()
        try:
            cookie.load(handler.headers.get("Cookie", ""))
        except Exception:  # noqa: BLE001
            return
        morsel = cookie.get(SESSION_COOKIE)
        if morsel:
            self.sessions.drop(morsel.value)


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        data: DashboardData,
        *,
        token: str,
        no_auth: bool,
        allow_controls: bool,
    ) -> None:
        self.data = data
        self.auth = DashboardAuth(token, no_auth=no_auth)
        self.allow_controls = allow_controls
        self._login_lock = threading.Lock()
        self._login_attempts: dict[str, list[float]] = {}
        super().__init__(address, DashboardHandler)

    def allow_login(self, ip: str) -> bool:
        now = time.monotonic()
        with self._login_lock:
            attempts = [t for t in self._login_attempts.get(ip, []) if now - t < 60.0]
            if len(attempts) >= 10:
                self._login_attempts[ip] = attempts
                return False
            attempts.append(now)
            self._login_attempts[ip] = attempts
            return True


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer
    protocol_version = "HTTP/1.1"
    server_version = "Krabs"
    sys_version = ""

    # -- HTTP methods ----------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/":
                if self._query_login(parsed):
                    return
                ok, _, _ = self.server.auth.authorize(self, cookie_only=True)
                if not ok:
                    self._send_login(HTTPStatus.UNAUTHORIZED)
                    return
                self._send_static("index.html")
                return

            if path == "/login":
                self._send_login(HTTPStatus.OK)
                return
            if path == "/logout":
                self._redirect("/login")
                return
            if path.startswith("/static/"):
                self._send_static(path.removeprefix("/static/"))
                return
            if path == "/favicon.ico":
                self._send_static("favicon.svg")
                return
            if path.startswith("/api/"):
                self._handle_api_get(parsed)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except Exception:  # noqa: BLE001 - never crash the dashboard on a bad request
            LOG.exception("GET %s failed", self.path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/login":
                self._handle_login()
                return
            if path == "/logout":
                self.server.auth.logout(self)
                self._redirect("/login", clear=True)
                return
            if path == "/api/kill":
                self._handle_kill()
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except Exception:  # noqa: BLE001
            LOG.exception("POST %s failed", self.path)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"})

    # -- routes ----------------------------------------------------------
    def _query_login(self, parsed) -> bool:
        token = parse_qs(parsed.query).get("token", [None])[0]
        if token and self.server.auth.token_ok(token):
            session, csrf = self.server.auth.create_session()
            self._redirect("/", session=session, csrf=csrf)
            return True
        if token:
            self._send_login(HTTPStatus.UNAUTHORIZED, error="Invalid token.")
            return True
        if self.server.auth.no_auth:
            return False
        return False

    def _handle_login(self) -> None:
        if self.server.auth.no_auth:
            self._redirect("/")
            return
        ip = self.client_address[0]
        if not self.server.allow_login(ip):
            self._send_login(HTTPStatus.TOO_MANY_REQUESTS, error="Too many attempts. Wait a minute.")
            return
        body = self._read_body()
        fields = parse_qs(body.decode("utf-8", "replace"))
        token = (fields.get("token") or [""])[0]
        if not self.server.auth.token_ok(token):
            self._redirect("/login?error=1")
            return
        session, csrf = self.server.auth.create_session()
        self._redirect("/", session=session, csrf=csrf)

    def _handle_api_get(self, parsed) -> None:
        ok, _, _ = self.server.auth.authorize(self)
        if not ok:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "authentication required"})
            return
        if parsed.path == "/api/snapshot":
            payload = self.server.data.snapshot()
            payload["controls"] = {"enabled": self.server.allow_controls}
            self._send_json(HTTPStatus.OK, payload)
            return
        if parsed.path == "/api/health":
            payload = self.server.data.snapshot()
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": not payload["heartbeat_stale"] and not payload["kill"]["engaged"],
                    "heartbeat_age_s": payload["heartbeat_age_s"],
                    "halted": (payload.get("heartbeat") or {}).get("halted", False),
                    "kill": payload["kill"]["engaged"],
                    "warnings": payload["warnings"],
                },
            )
            return
        if parsed.path == "/api/report":
            relative = parse_qs(parsed.query).get("path", [""])[0]
            try:
                payload = self.server.data.read_report(relative)
            except FileNotFoundError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "report not found"})
                return
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.OK, payload)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _handle_kill(self) -> None:
        if not self.server.allow_controls:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "controls disabled; restart with --allow-controls"},
            )
            return
        ok, csrf, kind = self.server.auth.authorize(self)
        if not ok:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "authentication required"})
            return
        if kind == "cookie":
            header = self.headers.get("X-Krabs-CSRF", "")
            if not csrf or not secrets.compare_digest(header, csrf):
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "invalid CSRF token"})
                return
        try:
            payload = json.loads(self._read_body() or b"{}")
        except (ValueError, TypeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            return
        action = str(payload.get("action", "")).lower()
        reason = str(payload.get("reason", "dashboard"))[:200] or "dashboard"
        if action == "engage":
            self.server.data.kill.engage(reason)
        elif action == "clear":
            self.server.data.kill.clear()
        else:
            self._send_json(
                HTTPStatus.BAD_REQUEST, {"error": "action must be engage or clear"}
            )
            return
        LOG.warning("dashboard kill switch action=%s reason=%s", action, reason)
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "action": action, "kill": self.server.data._kill_state()},
        )

    # -- responses -------------------------------------------------------
    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(_clean(payload), separators=(",", ":"), allow_nan=False).encode()
        self._send(status, body, "application/json; charset=utf-8")

    def _send_static(self, name: str) -> None:
        allowed = {
            "index.html": "text/html; charset=utf-8",
            "login.html": "text/html; charset=utf-8",
            "app.js": "text/javascript; charset=utf-8",
            "style.css": "text/css; charset=utf-8",
            "favicon.svg": "image/svg+xml",
            "manifest.webmanifest": "application/manifest+json",
        }
        if name not in allowed:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            return
        try:
            body = (STATIC_DIR / name).read_bytes()
        except OSError:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")
            return
        if name == "login.html":
            error = parse_qs(urlparse(self.path).query).get("error")
            message = (
                b'<p class="form-error">Invalid token. Try again.</p>'
                if error
                else b""
            )
            body = body.replace(b"{{ERROR}}", message)
        self._send(
            HTTPStatus.OK,
            body,
            allowed[name],
            cache="max-age=300" if name not in ("index.html", "login.html") else "no-store",
        )

    def _send_login(self, status: int, *, error: str = "") -> None:
        if not error and parse_qs(urlparse(self.path).query).get("error"):
            error = "Invalid token. Try again."
        try:
            body = (STATIC_DIR / "login.html").read_bytes()
        except OSError:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "login page missing"})
            return
        message = f'<p class="form-error">{_escape(error)}</p>'.encode() if error else b""
        body = body.replace(b"{{ERROR}}", message)
        self._send(status, body, "text/html; charset=utf-8")

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        cache: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self._security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; script-src 'self'; "
            "style-src 'self'; connect-src 'self'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'",
        )

    def _redirect(
        self,
        location: str,
        *,
        session: str | None = None,
        csrf: str | None = None,
        clear: bool = False,
    ) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        secure = self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        suffix = "; Secure" if secure else ""
        if session and csrf:
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}={session}; Path=/; HttpOnly; SameSite=Strict; "
                f"Max-Age={SESSION_TTL}{suffix}",
            )
            self.send_header(
                "Set-Cookie",
                f"{CSRF_COOKIE}={csrf}; Path=/; SameSite=Strict; "
                f"Max-Age={SESSION_TTL}{suffix}",
            )
        if clear:
            self.send_header(
                "Set-Cookie",
                f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",
            )
            self.send_header(
                "Set-Cookie", f"{CSRF_COOKIE}=; Path=/; SameSite=Strict; Max-Age=0"
            )
        self.end_headers()

    # -- helpers ---------------------------------------------------------
    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(length) if length > 0 else b""

    def log_message(self, fmt: str, *args: Any) -> None:
        safe = self.path.split("?", 1)[0]
        LOG.info("%s %s %s", self.client_address[0], self.command, safe)


def serve(
    data: DashboardData,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    token: str = "",
    no_auth: bool = False,
    allow_controls: bool = False,
) -> None:
    server = DashboardHTTPServer(
        (host, port),
        data,
        token=token,
        no_auth=no_auth,
        allow_controls=allow_controls,
    )
    actual_host, actual_port = server.server_address[:2]
    display = "127.0.0.1" if actual_host in ("0.0.0.0", "::") else actual_host
    LOG.info("dashboard listening on http://%s:%s/", display, actual_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("dashboard stopped")
    finally:
        server.server_close()


def _clean(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
