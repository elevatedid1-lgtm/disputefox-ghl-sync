"""Minimal HighLevel (GHL / LeadConnector) API v2 client.

Only the endpoints below are used. All are in the official OpenAPI spec
(github.com/GoHighLevel/highlevel-api-docs, apps/contacts.json), API version 2021-07-28:

  GET  /contacts/search/duplicate?locationId=&email=   (scope contacts.readonly)
  GET  /contacts/search/duplicate?locationId=&number=  (scope contacts.readonly)
  POST /contacts/                                      (scope contacts.write)
  PUT  /contacts/{contactId}                           (scope contacts.write)
  POST /contacts/{contactId}/tags                      (scope contacts.write)

/contacts/upsert is deliberately NOT used: when email and phone point at two
different contacts it silently picks one, and we must flag that instead.
Nothing here sends messages or deletes anything.
"""
import json
import logging
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

log = logging.getLogger("dfghl.ghl")


class GHLError(Exception):
    def __init__(self, status, message, retryable, retry_after=None):
        super().__init__(f"GHL {status}: {message}")
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


class RateLimiter:
    """Sliding window: at most `limit` requests per `window` seconds (GHL allows 100 per 10s per location)."""

    def __init__(self, limit, window=10.0):
        self.limit = max(1, limit)
        self.window = window
        self.calls = deque()
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            while True:
                now = time.monotonic()
                while self.calls and now - self.calls[0] >= self.window:
                    self.calls.popleft()
                if len(self.calls) < self.limit:
                    self.calls.append(now)
                    return
                time.sleep(self.window - (now - self.calls[0]) + 0.01)


class GHLClient:
    def __init__(self, cfg, quick_retries=3):
        self.base = cfg.ghl_base_url
        self.location_id = cfg.ghl_location_id
        self.timeout = cfg.http_timeout
        self._headers = {
            "Authorization": f"Bearer {cfg.ghl_api_token}",
            "Version": cfg.ghl_api_version,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "dfghl/1.0",
        }
        self.limiter = RateLimiter(cfg.rate_limit_per_10s)
        self.quick_retries = quick_retries

    def __repr__(self):
        return f"GHLClient(base={self.base!r}, location_id={self.location_id!r}, token=***)"

    # ---- transport ----------------------------------------------------
    def _request(self, method, path, query=None, body=None):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        last = None
        for attempt in range(self.quick_retries + 1):
            self.limiter.wait()
            req = urllib.request.Request(url, data=data, method=method, headers=self._headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                last = self._to_error(exc)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                last = GHLError(0, f"network error: {type(exc).__name__}", retryable=True)
            if not last.retryable or attempt == self.quick_retries:
                raise last
            delay = last.retry_after if last.retry_after is not None else min(30, 2 ** attempt + random.random())
            log.warning("GHL %s %s -> %s; retrying in %.1fs", method, path.split("?")[0], last.status, delay)
            time.sleep(delay)
        raise last  # pragma: no cover

    @staticmethod
    def _to_error(exc):
        status = exc.code
        try:
            payload = json.loads(exc.read() or b"{}")
            message = payload.get("message") or payload.get("error") or ""
            if isinstance(message, list):
                message = "; ".join(map(str, message))
        except Exception:
            message = ""
        retry_after = None
        header = exc.headers.get("Retry-After") if exc.headers else None
        if header:
            try:
                retry_after = min(120.0, float(header))
            except ValueError:
                pass
        retryable = status == 429 or status >= 500 or status == 408
        return GHLError(status, str(message)[:300] or exc.reason, retryable, retry_after)

    # ---- endpoints ----------------------------------------------------
    @staticmethod
    def _contact_id(resp):
        contact = resp.get("contact") if isinstance(resp, dict) else None
        if isinstance(contact, dict) and contact.get("id"):
            return contact["id"]
        return None

    def find_duplicate(self, email=None, phone=None):
        """Return the GHL contact id matching exactly one identifier, or None."""
        query = {"locationId": self.location_id}
        if email:
            query["email"] = email
        if phone:
            query["number"] = phone
        return self._contact_id(self._request("GET", "/contacts/search/duplicate", query=query))

    def create_contact(self, fields):
        body = dict(fields, locationId=self.location_id, source="DisputeFox")
        contact_id = self._contact_id(self._request("POST", "/contacts/", body=body))
        if not contact_id:
            raise GHLError(0, "create contact returned no id", retryable=False)
        return contact_id

    def update_contact(self, contact_id, fields):
        self._request("PUT", f"/contacts/{urllib.parse.quote(contact_id)}", body=fields)

    def add_tags(self, contact_id, tags):
        self._request("POST", f"/contacts/{urllib.parse.quote(contact_id)}/tags", body={"tags": list(tags)})
