"""Minimal read-only HTTP server, stdlib only (SPEC.md: 'no framework beyond
a minimal web server'). No POST routes exist - the terminal reads, it never
writes and never causes an order.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from src.log.db import connect
from src.server import api

TERMINAL_HTML = Path(__file__).resolve().parents[2] / "terminal" / "index.html"


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_terminal(self) -> None:
        body = TERMINAL_HTML.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        conn = connect()
        try:
            if parsed.path in ("/", "/index.html"):
                self._serve_terminal()
            elif parsed.path == "/api/decisions":
                limit = int(qs.get("limit", ["200"])[0])
                self._json(api.get_decisions(conn, limit))
            elif parsed.path.startswith("/api/decisions/"):
                decision_id = parsed.path.removeprefix("/api/decisions/")
                result = api.get_decision(conn, decision_id)
                self._json(result, status=200) if result else self._json({"error": "not found"}, status=404)
            elif parsed.path == "/api/funnel":
                self._json(api.get_funnel(conn, qs.get("date", ["today"])[0]))
            elif parsed.path == "/api/positions":
                self._json(api.get_positions(conn))
            elif parsed.path == "/api/scorecard":
                self._json(api.get_scorecard(conn))
            else:
                self._json({"error": "not found"}, status=404)
        finally:
            conn.close()

    def log_message(self, format: str, *args) -> None:
        pass  # quiet by default; nothing sensitive should ever reach here either way


def run(host: str = "127.0.0.1", port: int = 8787) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"kronos-1h terminal API on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run()
