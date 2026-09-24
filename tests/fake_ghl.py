"""In-memory stand-in for the four GHL endpoints we use. Records every request."""
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TOKEN = "test-ghl-token-abcdefghijklmnop"
LOCATION = "loc123"


class FakeGHL:
    def __init__(self):
        self.contacts = {}
        self.requests = []
        self.fail_queue = []  # list of (status, retry_after) returned before normal handling
        self.lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, status, payload, headers=None):
                body = json.dumps(payload).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self):
                parts = urlsplit(self.path)
                q = {k: v[-1] for k, v in parse_qs(parts.query).items()}
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                with fake.lock:
                    fake.requests.append((self.command, parts.path, q, body))
                    if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                        return self._reply(401, {"message": "Invalid JWT"})
                    if self.headers.get("Version") != "2021-07-28":
                        return self._reply(400, {"message": "version header missing"})
                    if fake.fail_queue:
                        status, retry_after = fake.fail_queue.pop(0)
                        return self._reply(status, {"message": "forced"},
                                           {"Retry-After": str(retry_after)} if retry_after is not None else None)
                    return fake.route(self, self.command, parts.path, q, body)

            do_GET = do_POST = do_PUT = do_DELETE = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def add(self, **fields):
        cid = uuid.uuid4().hex[:20]
        self.contacts[cid] = dict(fields, id=cid, tags=[])
        return cid

    def writes(self):
        return [r for r in self.requests if r[0] in ("POST", "PUT", "DELETE")]

    def route(self, h, method, path, q, body):
        if method == "GET" and path == "/contacts/search/duplicate":
            assert q.get("locationId") == LOCATION
            for c in self.contacts.values():
                if q.get("email") and c.get("email") == q["email"]:
                    return h._reply(200, {"contact": c})
                if q.get("number") and c.get("phone") == q["number"]:
                    return h._reply(200, {"contact": c})
            return h._reply(200, {"contact": None})
        if method == "POST" and path == "/contacts/":
            assert body["locationId"] == LOCATION
            fields = {k: v for k, v in body.items() if k != "locationId"}
            cid = self.add(**fields)
            return h._reply(201, {"contact": self.contacts[cid]})
        if path.startswith("/contacts/"):
            segs = path.strip("/").split("/")
            cid = segs[1]
            if cid not in self.contacts:
                return h._reply(400, {"message": "Contact not found"})
            if method == "PUT" and len(segs) == 2:
                self.contacts[cid].update(body)
                return h._reply(200, {"succeded": True, "contact": self.contacts[cid]})
            if method == "POST" and segs[2:] == ["tags"]:
                tags = self.contacts[cid]["tags"]
                tags.extend(t for t in body["tags"] if t not in tags)
                return h._reply(201, {"tags": tags})
        return h._reply(404, {"message": "no route"})
