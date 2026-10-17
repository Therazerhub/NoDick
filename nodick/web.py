"""Single-process health server and authenticated Telegram Mini App API."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlparse

from nodick.config import settings
from nodick.db import (
    get_catalog,
    get_categories,
    get_favorites,
    get_user_dashboard,
    get_video,
    record_growth_event,
    set_favorite,
)
from nodick.utils import clean_title_for_display, is_placeholder_title

log = logging.getLogger(__name__)
ASSET_ROOT = Path(__file__).resolve().parent / "webapp"
MAX_INIT_DATA_AGE = 3600


class AuthError(ValueError):
    """Raised when Telegram Web App authentication cannot be trusted."""


def validate_init_data(init_data: str, bot_token: str, *, now: int | None = None, max_age: int = MAX_INIT_DATA_AGE) -> dict:
    """Validate Telegram initData using the documented WebAppData HMAC flow."""
    if not init_data or len(init_data) > 8192 or not bot_token:
        raise AuthError("missing authentication")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise AuthError("invalid authentication payload") from None
    values: dict[str, str] = {}
    for key, value in pairs:
        if key in values:
            raise AuthError("duplicate authentication field")
        values[key] = value
    received_hash = values.pop("hash", "")
    if len(received_hash) != 64:
        raise AuthError("invalid authentication hash")
    data_check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise AuthError("invalid authentication hash")
    try:
        auth_date = int(values["auth_date"])
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise AuthError("invalid authentication payload") from None
    current = int(time.time() if now is None else now)
    if auth_date > current + 30 or current - auth_date > max_age:
        raise AuthError("expired authentication")
    if user_id <= 0:
        raise AuthError("invalid user")
    return {"user_id": user_id, "user": user, "auth_date": auth_date, "query_id": values.get("query_id")}


def _safe_video(row: dict, bot_username: str) -> dict:
    """Expose polished text metadata and a deep link, never media references."""
    raw_title = str(row.get("title") or "").strip()
    category = str(row.get("category") or "Other").strip() or "Other"
    if raw_title and not is_placeholder_title(raw_title):
        title = clean_title_for_display(raw_title)
    else:
        title = f"{category} selection" if category != "Other" else "Library selection"
    return {
        "id": int(row["id"]),
        "title": title,
        "category": category,
        "duration": int(row.get("duration") or 0),
        "saved": bool(row.get("saved", False)),
        "open_url": f"https://t.me/{bot_username}?start=vid_{int(row['id'])}",
    }


def make_handler(*, bot_token: str | None = None, bot_username: str | None = None):
    token = settings.bot_token if bot_token is None else bot_token
    username = (bot_username or settings.bot_username).lstrip("@")

    class MiniAppHandler(BaseHTTPRequestHandler):
        server_version = "NoDickWeb/1"
        timeout = 20

        def _headers(self, status: int, content_type: str, length: int = 0) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store" if content_type.startswith("application/json") else "public, max-age=300")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            # Telegram Desktop/Web may embed Mini Apps cross-origin. Use the
            # explicit CSP allowlist; X-Frame-Options: SAMEORIGIN would block it.
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'none'; media-src 'none'; frame-ancestors https://web.telegram.org https://*.telegram.org")
            if length:
                self.send_header("Content-Length", str(length))
            self.end_headers()

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self._headers(status, content_type, len(body))
            self.wfile.write(body)

        def _json(self, status: int, value: dict | list) -> None:
            self._send(status, json.dumps(value, separators=(",", ":"), default=str).encode(), "application/json; charset=utf-8")

        def _auth(self) -> dict | None:
            header = self.headers.get("Authorization", "")
            init_data = header[4:].strip() if header.lower().startswith("tma ") else self.headers.get("X-Telegram-Init-Data", "")
            try:
                return validate_init_data(init_data, token)
            except AuthError as exc:
                self._json(401, {"error": str(exc)})
                return None

        def _mutation_origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin or not settings.mini_app_url:
                return True
            expected = urlparse(settings.mini_app_url)
            actual = urlparse(origin)
            return (actual.scheme, actual.netloc) == (expected.scheme, expected.netloc)

        def do_GET(self) -> None:  # noqa: N802
            started = time.monotonic()
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/":
                self._send(200, b"NoDick is alive", "text/plain")
                return
            if path in ("/app", "/app/"):
                self._serve_asset("index.html")
                return
            if path.startswith("/app/assets/"):
                name = path.removeprefix("/app/assets/")
                if name in {"app.js", "style.css"}:
                    self._serve_asset(name)
                    return
                self._json(404, {"error": "not found"})
                return
            if not path.startswith("/api/"):
                self._json(404, {"error": "not found"})
                return
            auth = self._auth()
            if not auth:
                return
            user_id = auth["user_id"]
            query = parse_qs(parsed.query)
            try:
                if path == "/api/dashboard":
                    # Analytics must never prevent the dashboard from opening.
                    try:
                        record_growth_event("mini_app_open", user_id, source="mini_app")
                    except Exception:
                        log.exception("Mini App growth event failed for user %s", user_id)
                    self._json(200, get_user_dashboard(user_id))
                elif path == "/api/categories":
                    self._json(200, {"categories": [dict(row) for row in get_categories()]})
                elif path == "/api/catalog":
                    try:
                        page = max(0, min(10_000, int(query.get("page", ["0"])[0])))
                    except ValueError:
                        self._json(400, {"error": "invalid page"})
                        return
                    q = query.get("q", [""])[0][:100]
                    category = query.get("category", [""])[0][:100]
                    rows, total = get_catalog(user_id=user_id, query=q, category=category, page=page)
                    self._json(200, {"items": [_safe_video(dict(row), username) for row in rows], "total": total, "page": page})
                elif path == "/api/saved":
                    try:
                        page = max(0, min(10_000, int(query.get("page", ["0"])[0])))
                    except ValueError:
                        self._json(400, {"error": "invalid page"})
                        return
                    rows, total = get_favorites(user_id, page=page, per_page=50)
                    self._json(200, {"items": [_safe_video({**dict(row), "saved": True}, username) for row in rows], "total": total, "page": page})
                else:
                    self._json(404, {"error": "not found"})
            except Exception:
                log.exception("Mini App API failed: path=%s user=%s", path, user_id)
                self._json(500, {"error": "Library service is temporarily unavailable. Please retry."})
            finally:
                elapsed = time.monotonic() - started
                log.info("Mini App API: path=%s user=%s elapsed=%.3fs", path, user_id, elapsed)

        def do_POST(self) -> None:  # noqa: N802
            self._saved_mutation(True)

        def do_DELETE(self) -> None:  # noqa: N802
            self._saved_mutation(False)

        def _saved_mutation(self, saved: bool) -> None:
            path = urlparse(self.path).path
            if not path.startswith("/api/saved/"):
                self._json(404, {"error": "not found"})
                return
            auth = self._auth()
            if not auth:
                return
            if not self._mutation_origin_ok():
                self._json(403, {"error": "origin denied"})
                return
            try:
                video_id = int(path.rsplit("/", 1)[1])
            except ValueError:
                self._json(400, {"error": "invalid video"})
                return
            if video_id <= 0 or not get_video(video_id):
                self._json(404, {"error": "video not found"})
                return
            set_favorite(auth["user_id"], video_id, saved)
            self._json(200, {"video_id": video_id, "saved": saved})

        def _serve_asset(self, name: str) -> None:
            path = ASSET_ROOT / name
            try:
                body = path.read_bytes()
            except OSError:
                self._json(404, {"error": "not found"})
                return
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self._send(200, body, f"{content_type}; charset=utf-8")

        def log_message(self, format: str, *args) -> None:
            log.debug("Mini App: " + format, *args)

    return MiniAppHandler


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with bounded concurrency and request timeouts."""

    MAX_REQUEST_THREADS = 12
    REQUEST_TIMEOUT = 20

    def __init__(self, *args, **kwargs):
        self._semaphore = threading.BoundedSemaphore(self.MAX_REQUEST_THREADS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        conn = request[1] if isinstance(request, tuple) else request
        if not self._semaphore.acquire(blocking=False):
            # Shed load instead of piling up threads and database connections.
            try:
                conn.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: text/plain\r\n"
                    b"Connection: close\r\n"
                    b"Content-Length: 23\r\n\r\n"
                    b"Server too busy, retry soon"
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            # The timeout belongs to the accepted socket/handler, not the
            # server object. Without this, stalled clients hold threads forever.
            conn = request[1] if isinstance(request, tuple) else request
            conn.settimeout(self.REQUEST_TIMEOUT)
            super().process_request_thread(request, client_address)
        finally:
            self._semaphore.release()

    def handle_error(self, request, client_address):
        log.debug("Mini App request error from %s", client_address, exc_info=True)


def start_web_server(port: int) -> BoundedThreadingHTTPServer:
    """Start health, static and API routes beside PTB polling."""
    server = BoundedThreadingHTTPServer(("0.0.0.0", port), make_handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="mini-app-http")
    thread.start()
    return server
