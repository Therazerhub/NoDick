"""Render health endpoint — the bot is pure Telegram again."""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)


def make_handler():
    class HealthHandler(BaseHTTPRequestHandler):
        server_version = "NoDickHealth/1"

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "14")
            self.end_headers()
            self.wfile.write(b"NoDick is alive")

        def log_message(self, format: str, *args) -> None:
            log.debug("Health: " + format, *args)

    return HealthHandler


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Health server with bounded concurrency and request timeouts."""

    MAX_REQUEST_THREADS = 8
    REQUEST_TIMEOUT = 20

    def __init__(self, *args, **kwargs):
        self._semaphore = threading.BoundedSemaphore(self.MAX_REQUEST_THREADS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        conn = request[1] if isinstance(request, tuple) else request
        if not self._semaphore.acquire(blocking=False):
            try:
                conn.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            conn = request[1] if isinstance(request, tuple) else request
            conn.settimeout(self.REQUEST_TIMEOUT)
            super().process_request_thread(request, client_address)
        finally:
            self._semaphore.release()

    def handle_error(self, request, client_address):
        log.debug("Health request error from %s", client_address, exc_info=True)


def start_web_server(port: int) -> BoundedThreadingHTTPServer:
    """Start the Render health endpoint beside PTB polling."""
    server = BoundedThreadingHTTPServer(("0.0.0.0", port), make_handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="health-http")
    thread.start()
    return server
