"""HTTPS-facing receiver for the DisputeFox AutoFox "API Action", plus a background worker.

POST /webhooks/disputefox   <- DisputeFox calls this (JSON or form body)
GET  /healthz               <- uptime checks

Auth: the shared WEBHOOK_SECRET, sent as any one of
  - header  X-Webhook-Token: <secret>
  - header  Authorization: Bearer <secret>
  - query   ?token=<secret>   (for tools that can't set headers)

The receiver only validates, queues and returns 202. GHL calls happen in the worker,
so a slow or rate-limited GHL never makes DisputeFox time out or re-send.
"""
import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .normalize import PayloadError, extract, flatten, parse_body

log = logging.getLogger("dfghl.server")
WEBHOOK_PATH = "/webhooks/disputefox"


class Worker(threading.Thread):
    def __init__(self, syncer, poll_seconds=15):
        super().__init__(daemon=True, name="dfghl-worker")
        self.syncer = syncer
        self.poll = poll_seconds
        self.wake = threading.Event()
        self.stopping = threading.Event()

    def run(self):
        while not self.stopping.is_set():
            try:
                results = self.syncer.run_due()
                if results:
                    log.info("worker batch: %s", results)
            except Exception:
                log.exception("worker loop error")
            self.wake.wait(self.poll)
            self.wake.clear()

    def stop(self):
        self.stopping.set()
        self.wake.set()


def make_handler(cfg, store, worker):
    secret = cfg.webhook_secret.encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "dfghl"
        sys_version = ""

        def log_message(self, fmt, *args):
            # Never log the query string: it may carry the token.
            log.info("%s %s %s", self.command, urlsplit(self.path).path, args[1] if len(args) > 1 else "")

        def _send(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self, query):
            candidates = [self.headers.get("X-Webhook-Token", "")]
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                candidates.append(auth[7:].strip())
            candidates.extend(query.get("token", []))
            return any(c and hmac.compare_digest(c.encode(), secret) for c in candidates)

        def do_GET(self):
            if urlsplit(self.path).path == "/healthz":
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            parts = urlsplit(self.path)
            if parts.path != WEBHOOK_PATH:
                return self._send(404, {"error": "not found"})
            query = parse_qs(parts.query)
            if not self._authorized(query):
                log.warning("rejected webhook: bad or missing token from %s", self.client_address[0])
                return self._send(401, {"error": "unauthorized"})

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, {"error": "bad Content-Length"})
            if length > cfg.max_body_bytes:
                return self._send(413, {"error": "body too large"})
            raw = self.rfile.read(length) if length else b""

            data = {}
            try:
                data = parse_body(raw, self.headers.get("Content-Type", ""))
                # Fields may also arrive in the query string (GET-style API actions).
                for k, v in query.items():
                    if k != "token":
                        data.setdefault(k, v[-1])
                record, notes = extract(data, cfg.default_country_code, cfg.allow_email_as_client_id)
            except PayloadError as exc:
                keys = sorted(flatten(data).keys())
                log.warning("rejected webhook payload: %s | field names received: %s", exc, keys)
                return self._send(422, {"error": str(exc), "fields_received": keys})

            event_id, duplicate = store.enqueue(record, notes)
            log.info("queued event %s for client %s%s%s", event_id, record.client_id,
                     " (duplicate, already queued)" if duplicate else "",
                     f" notes: {'; '.join(notes)}" if notes else "")
            if worker:
                worker.wake.set()
            self._send(202, {"ok": True, "event_id": event_id, "duplicate": duplicate})

    return Handler


def serve(cfg, store, syncer):
    worker = Worker(syncer)
    worker.start()
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(cfg, store, worker))
    mode = "DRY RUN" if cfg.dry_run else "LIVE"
    if cfg.test_mode:
        mode += " / SINGLE-CLIENT TEST MODE"
    log.info("listening on %s:%s%s  [%s]", cfg.host, cfg.port, WEBHOOK_PATH, mode)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()
        httpd.server_close()
