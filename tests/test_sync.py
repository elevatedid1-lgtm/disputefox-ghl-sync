import io
import json
import logging
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.request

os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

from dfghl.__main__ import RedactFilter, main as cli_main
from dfghl.config import Config
from dfghl.ghl import GHLClient
from dfghl.normalize import PayloadError, extract, normalize_phone, parse_body
from dfghl.server import make_handler
from dfghl.store import Store
from dfghl.sync import Syncer
from tests.fake_ghl import LOCATION, TOKEN, FakeGHL

SECRET = "webhook-secret-0123456789-abcdefghij"


def rec(client_id="DF1", first="Ana", last="Diaz", email="ana@example.com", phone="(555) 123-4567"):
    record, notes = extract({"client_id": client_id, "first_name": first, "last_name": last,
                             "email": email, "phone": phone})
    return record, notes


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.ghl = FakeGHL()
        self.cfg = Config(ghl_api_token=TOKEN, ghl_location_id=LOCATION, webhook_secret=SECRET,
                          ghl_base_url=self.ghl.url, db_path=":memory:")
        self.store = Store(":memory:")
        self.client = GHLClient(self.cfg, quick_retries=2)
        self.syncer = Syncer(self.cfg, self.store, self.client)

    def tearDown(self):
        self.ghl.stop()

    def push(self, **kw):
        record, notes = rec(**kw)
        event_id, _ = self.store.enqueue(record, notes)
        self.syncer.run_due()
        return self.store.get_event(event_id)

    def test_creates_new_contact_tags_and_maps(self):
        ev = self.push()
        self.assertEqual(ev["status"], "done")
        self.assertEqual(ev["action"], "create")
        c = self.ghl.contacts[ev["ghl_contact_id"]]
        self.assertEqual((c["firstName"], c["lastName"], c["email"], c["phone"]),
                         ("Ana", "Diaz", "ana@example.com", "+15551234567"))
        self.assertEqual(c["tags"], ["disputefox-synced"])
        self.assertEqual(c["source"], "DisputeFox")
        self.assertEqual(self.store.get_mapping("DF1")["ghl_contact_id"], ev["ghl_contact_id"])

    def test_writes_client_id_custom_field_when_configured(self):
        self.cfg.ghl_client_id_field = "cf123"
        ev = self.push()
        c = self.ghl.contacts[ev["ghl_contact_id"]]
        self.assertEqual(c["customFields"], [{"id": "cf123", "field_value": "DF1"}])
        ev2 = self.push(phone="555-999-0000")
        update_body = [r for r in self.ghl.requests if r[0] == "PUT"][-1][3]
        self.assertEqual(update_body["customFields"], [{"id": "cf123", "field_value": "DF1"}])

    def test_writes_affiliate_custom_field_when_configured(self):
        self.cfg.ghl_client_id_field = "cf123"
        self.cfg.ghl_affiliate_field = "cfAff"
        record, notes = extract({"client_id": "DF9", "email": "aff@example.com", "affiliate": "Smile Center - Rep Jo"})
        self.store.enqueue(record, notes)
        self.syncer.run_due()
        contact = next(c for c in self.ghl.contacts.values() if c.get("email") == "aff@example.com")
        self.assertEqual(contact["customFields"], [{"id": "cf123", "field_value": "DF9"},
                                                   {"id": "cfAff", "field_value": "Smile Center - Rep Jo"}])

    def test_fingerprint_unchanged_without_affiliate(self):
        record, _ = rec()
        self.assertEqual(record.affiliate, "")
        import hashlib
        legacy = hashlib.sha256(json.dumps([record.client_id, record.first_name, record.last_name,
                                            record.email, record.phone]).encode()).hexdigest()
        self.assertEqual(record.fingerprint(), legacy)

    def test_no_custom_field_by_default_or_for_email_keys(self):
        ev = self.push()
        self.assertNotIn("customFields", self.ghl.contacts[ev["ghl_contact_id"]])
        record, _ = extract({"email": "z@example.com"}, allow_email_as_client_id=True)
        self.assertNotIn("customFields", record.ghl_fields("cf123"))

    def test_repeat_event_creates_no_duplicate_and_no_writes(self):
        self.push()
        writes_before = len(self.ghl.writes())
        ev = self.push()
        self.assertEqual(ev["action"], "no_change")
        self.assertEqual(len(self.ghl.contacts), 1)
        self.assertEqual(len(self.ghl.writes()), writes_before)

    def test_duplicate_pending_event_is_queued_once(self):
        record, notes = rec()
        a, dup_a = self.store.enqueue(record, notes)
        b, dup_b = self.store.enqueue(record, notes)
        self.assertEqual(a, b)
        self.assertTrue(dup_b)

    def test_update_uses_mapping(self):
        first = self.push()
        ev = self.push(phone="555-999-0000")
        self.assertEqual(ev["action"], "update")
        self.assertEqual(ev["ghl_contact_id"], first["ghl_contact_id"])
        self.assertEqual(len(self.ghl.contacts), 1)
        self.assertEqual(self.ghl.contacts[first["ghl_contact_id"]]["phone"], "+15559990000")

    def test_links_existing_contact_by_email(self):
        existing = self.ghl.add(email="ana@example.com", firstName="A")
        ev = self.push(phone="")
        self.assertEqual(ev["action"], "link_update")
        self.assertEqual(ev["ghl_contact_id"], existing)
        self.assertEqual(len(self.ghl.contacts), 1)
        self.assertIn("disputefox-synced", self.ghl.contacts[existing]["tags"])

    def test_links_existing_contact_by_phone(self):
        existing = self.ghl.add(phone="+15551234567")
        ev = self.push(email="new@example.com")
        self.assertEqual(ev["ghl_contact_id"], existing)

    def test_conflict_goes_to_review_without_writes(self):
        self.ghl.add(email="ana@example.com")
        self.ghl.add(phone="+15551234567")
        ev = self.push()
        self.assertEqual(ev["status"], "review")
        self.assertEqual(self.ghl.writes(), [])
        self.assertIsNone(self.store.get_mapping("DF1"))
        reviews = self.store.list_reviews()
        self.assertEqual(len(reviews), 1)
        self.assertIn("two different", reviews[0]["reason"])
        # A later update for the same client is held while the review is open.
        ev2 = self.push(first="Anita")
        self.assertEqual(ev2["status"], "review")
        self.assertEqual(self.ghl.writes(), [])

    def test_contact_already_linked_to_other_client_goes_to_review(self):
        self.push(client_id="DF1")
        ev = self.push(client_id="DF2", phone="")  # same email, different DisputeFox client
        self.assertEqual(ev["status"], "review")
        self.assertIn("another DisputeFox client", self.store.list_reviews()[0]["reason"])

    def test_dry_run_reads_but_never_writes(self):
        self.cfg.dry_run = True
        ev = self.push()
        self.assertEqual(ev["status"], "dry_run")
        self.assertEqual(ev["action"], "would_create")
        self.assertEqual(self.ghl.writes(), [])
        self.assertIsNone(self.store.get_mapping("DF1"))
        # Turning dry run off and requeueing performs the real sync.
        self.cfg.dry_run = False
        self.store.requeue(status="dry_run")
        self.syncer.run_due()
        self.assertEqual(self.store.get_event(ev["id"])["status"], "done")

    def test_single_client_test_mode(self):
        self.cfg.test_client_id = "DF-TEST"
        skipped = self.push(client_id="DF1")
        self.assertEqual(skipped["status"], "skipped")
        self.assertEqual(self.ghl.writes(), [])
        ok = self.push(client_id="DF-TEST", email="t@example.com", phone="")
        self.assertEqual(ok["status"], "done")

    def test_rate_limit_429_is_retried(self):
        self.ghl.fail_queue = [(429, 0), (429, 0)]
        ev = self.push()
        self.assertEqual(ev["status"], "done")

    def test_server_errors_back_off_then_fail(self):
        self.cfg.max_attempts = 2
        self.client.quick_retries = 0
        self.ghl.fail_queue = [(500, 0)] * 10
        record, notes = rec()
        event_id, _ = self.store.enqueue(record, notes)
        self.syncer.run_due()
        ev = self.store.get_event(event_id)
        self.assertEqual(ev["status"], "pending")
        self.assertGreater(ev["next_attempt_at"], time.time() + 20)
        self.store.db.execute("UPDATE events SET next_attempt_at=0")
        self.syncer.run_due()
        ev = self.store.get_event(event_id)
        self.assertEqual(ev["status"], "failed")
        self.assertIn("500", ev["last_error"])
        # Manual retry once GHL recovers.
        self.ghl.fail_queue = []
        self.store.requeue(status="failed")
        self.syncer.run_due()
        self.assertEqual(self.store.get_event(event_id)["status"], "done")

    def test_bad_token_fails_without_retry(self):
        self.client._headers["Authorization"] = "Bearer wrong"
        ev = self.push()
        self.assertEqual(ev["status"], "failed")
        self.assertIn("401", ev["last_error"])
        self.assertEqual(len(self.ghl.requests), 1)

    def test_tag_failure_does_not_create_second_contact(self):
        self.client.quick_retries = 0
        # search email, search phone, create succeed; tag call fails.
        original = self.ghl.route
        calls = {"n": 0}

        def flaky(h, method, path, q, body):
            if path.endswith("/tags") and calls["n"] == 0:
                calls["n"] += 1
                return h._reply(503, {"message": "down"}, {"Retry-After": "0"})
            return original(h, method, path, q, body)

        self.ghl.route = flaky
        record, notes = rec()
        event_id, _ = self.store.enqueue(record, notes)
        self.syncer.run_due()
        self.assertEqual(self.store.get_event(event_id)["status"], "pending")
        self.store.db.execute("UPDATE events SET next_attempt_at=0")
        self.syncer.run_due()
        self.assertEqual(self.store.get_event(event_id)["status"], "done")
        self.assertEqual(len(self.ghl.contacts), 1)

    def test_no_delete_or_message_endpoints_are_ever_called(self):
        self.push()
        self.push(phone="555-000-1111")
        for method, path, _, _ in self.ghl.requests:
            self.assertNotEqual(method, "DELETE")
            self.assertNotIn("conversations", path)
            self.assertNotIn("upsert", path)

    def test_resolve_review_by_linking(self):
        a = self.ghl.add(email="ana@example.com")
        self.ghl.add(phone="+15551234567")
        with tempfile.TemporaryDirectory() as d:
            # Exercise the real CLI against a file database.
            db = os.path.join(d, "t.sqlite3")
            file_store = Store(db)
            record, notes = rec()
            file_store.enqueue(record, notes)
            Syncer(self.cfg, file_store, self.client).run_due()
            rid = file_store.list_reviews()[0]["id"]
            env = {"DB_PATH": db, "GHL_API_TOKEN": TOKEN, "GHL_LOCATION_ID": LOCATION,
                   "WEBHOOK_SECRET": SECRET, "GHL_BASE_URL": self.ghl.url, "DFGHL_ENV_FILE": "/nonexistent"}
            old = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                cli_main(["resolve", str(rid), "--link", a])
                cli_main(["process"])
            finally:
                for k, v in old.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
            self.assertEqual(Store(db).get_mapping("DF1")["ghl_contact_id"], a)
            self.assertIn("disputefox-synced", self.ghl.contacts[a]["tags"])
            self.assertEqual(Store(db).list_reviews("open"), [])


class NormalizeTests(unittest.TestCase):
    def test_phone(self):
        self.assertEqual(normalize_phone("(555) 123-4567"), "+15551234567")
        self.assertEqual(normalize_phone("1-555-123-4567"), "+15551234567")
        self.assertEqual(normalize_phone("+44 20 7946 0958"), "+442079460958")
        self.assertEqual(normalize_phone("12345"), "")
        self.assertEqual(normalize_phone("{{client.phone}}"), "")

    def test_aliases_nested_and_placeholders(self):
        r, notes = extract({"client": {"ClientID": 77, "FirstName": "Bo", "LastName": "Li",
                                       "Email": "BO@Example.COM", "Mobile": "5551112222"},
                            "ssn": "123-45-6789"})
        self.assertEqual((r.client_id, r.first_name, r.email, r.phone), ("77", "Bo", "bo@example.com", "+15551112222"))
        self.assertNotIn("ssn", json.dumps(r.__dict__))
        r, notes = extract({"client_id": "5", "email": "a@b.co", "phone": "{{phone}}", "first_name": "[first_name]"})
        self.assertEqual((r.phone, r.first_name), ("", ""))

    def test_missing_client_id_or_contact_info(self):
        with self.assertRaises(PayloadError):
            extract({"email": "a@b.co"})
        with self.assertRaises(PayloadError):
            extract({"client_id": "1", "email": "not-an-email"})
        r, notes = extract({"email": "a@b.co"}, allow_email_as_client_id=True)
        self.assertEqual(r.client_id, "email:a@b.co")

    def test_form_body(self):
        data = parse_body(b"client_id=9&email=x%40y.com&first_name=X", "application/x-www-form-urlencoded")
        self.assertEqual(extract(data)[0].email, "x@y.com")

    def test_field_override(self):
        os.environ["FIELD_CLIENT_ID"] = "account_number"
        try:
            r, _ = extract({"id": "event-1", "account_number": "A-9", "email": "a@b.co"})
            self.assertEqual(r.client_id, "A-9")
        finally:
            del os.environ["FIELD_CLIENT_ID"]


class ServerTests(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        import threading
        self.cfg = Config(ghl_api_token=TOKEN, ghl_location_id=LOCATION, webhook_secret=SECRET, db_path=":memory:")
        self.store = Store(":memory:")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.cfg, self.store, None))
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.logbuf = io.StringIO()
        self.loghandler = logging.StreamHandler(self.logbuf)
        self.loghandler.addFilter(RedactFilter([TOKEN, SECRET]))
        logging.getLogger().addHandler(self.loghandler)
        logging.getLogger().setLevel(logging.INFO)

    def tearDown(self):
        logging.getLogger().removeHandler(self.loghandler)
        self.httpd.shutdown()
        self.httpd.server_close()

    def post(self, path, body, headers=None, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method="POST",
                                     headers={"Content-Type": ctype, **(headers or {})})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_rejects_missing_or_wrong_token(self):
        body = {"client_id": "1", "email": "a@b.co"}
        self.assertEqual(self.post("/webhooks/disputefox", body)[0], 401)
        self.assertEqual(self.post("/webhooks/disputefox", body, {"X-Webhook-Token": "nope"})[0], 401)
        self.assertEqual(self.store.counts(), {})

    def test_accepts_header_bearer_and_query_token(self):
        s1, r1 = self.post("/webhooks/disputefox", {"client_id": "1", "email": "a@b.co"}, {"X-Webhook-Token": SECRET})
        s2, _ = self.post("/webhooks/disputefox", {"client_id": "2", "email": "b@b.co"},
                          {"Authorization": f"Bearer {SECRET}"})
        s3, _ = self.post(f"/webhooks/disputefox?token={SECRET}", b"client_id=3&email=c%40b.co",
                          ctype="application/x-www-form-urlencoded")
        self.assertEqual((s1, s2, s3), (202, 202, 202))
        self.assertTrue(r1["ok"])
        self.assertEqual(self.store.counts(), {"pending": 3})
        self.assertNotIn(SECRET, self.logbuf.getvalue())

    def test_bad_payload_reports_field_names_not_values(self):
        status, resp = self.post("/webhooks/disputefox", {"name": "Ana", "mail": "ana@example.com"},
                                 {"X-Webhook-Token": SECRET})
        self.assertEqual(status, 422)
        self.assertEqual(resp["fields_received"], ["mail", "name"])
        self.assertNotIn("ana@example.com", self.logbuf.getvalue())

    def test_healthz_and_unknown_path(self):
        with urllib.request.urlopen(self.url + "/healthz") as resp:
            self.assertEqual(json.loads(resp.read()), {"ok": True})
        self.assertEqual(self.post("/other", {}, {"X-Webhook-Token": SECRET})[0], 404)


if __name__ == "__main__":
    unittest.main()
