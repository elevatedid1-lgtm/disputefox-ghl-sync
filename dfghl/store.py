"""SQLite storage: the inbound event queue, the DisputeFox -> GHL ID map, and the review queue.

The database holds client names, emails and phone numbers. Keep it on a private
disk, back it up, and run `python -m dfghl purge` periodically.
"""
import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at     REAL NOT NULL,
    client_id       TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    record_json     TEXT,
    notes           TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    action          TEXT,
    ghl_contact_id  TEXT,
    last_error      TEXT,
    updated_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_due ON events(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_events_client ON events(client_id);

CREATE TABLE IF NOT EXISTS mappings (
    df_client_id     TEXT PRIMARY KEY,
    ghl_contact_id   TEXT NOT NULL UNIQUE,
    last_fingerprint TEXT,
    confirmed        INTEGER NOT NULL DEFAULT 0,  -- 1 = a person linked these by hand; skip conflict checks
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     INTEGER NOT NULL,
    df_client_id TEXT NOT NULL,
    reason       TEXT NOT NULL,
    candidates   TEXT,
    status       TEXT NOT NULL DEFAULT 'open',
    resolution   TEXT,
    created_at   REAL NOT NULL,
    resolved_at  REAL
);
CREATE INDEX IF NOT EXISTS idx_reviews_open ON reviews(status, df_client_id);
"""

# Statuses an event can end in. 'pending' is the only one the worker picks up.
FINAL = ("done", "skipped", "review", "failed", "dry_run")


class Store:
    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)
        if path != ":memory:":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass

    def _q(self, sql, args=()):
        with self._lock:
            return self.db.execute(sql, args)

    # ---- events -------------------------------------------------------
    def enqueue(self, record, notes):
        """Queue an event. Returns (event_id, is_duplicate).

        An identical record for the same client that is still pending is not queued twice,
        which absorbs DisputeFox re-sending the same action.
        """
        now = time.time()
        with self._lock:
            row = self._q("SELECT id FROM events WHERE client_id=? AND fingerprint=? AND status='pending'",
                          (record.client_id, record.fingerprint())).fetchone()
            if row:
                return row["id"], True
            cur = self._q(
                "INSERT INTO events(received_at, client_id, fingerprint, record_json, notes, status, updated_at) "
                "VALUES (?,?,?,?,?, 'pending', ?)",
                (now, record.client_id, record.fingerprint(), json.dumps(record.__dict__),
                 "; ".join(notes) or None, now))
            return cur.lastrowid, False

    def claim_due(self, limit=10):
        """Return pending events whose retry time has arrived, oldest first."""
        with self._lock:
            return self._q("SELECT * FROM events WHERE status='pending' AND next_attempt_at<=? "
                           "ORDER BY id LIMIT ?", (time.time(), limit)).fetchall()

    def get_event(self, event_id):
        return self._q("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()

    def finish(self, event_id, status, action=None, ghl_contact_id=None, error=None):
        self._q("UPDATE events SET status=?, action=?, ghl_contact_id=?, last_error=?, "
                "attempts=attempts+1, updated_at=? WHERE id=?",
                (status, action, ghl_contact_id, error, time.time(), event_id))

    def retry_later(self, event_id, error, delay):
        self._q("UPDATE events SET attempts=attempts+1, last_error=?, next_attempt_at=?, updated_at=? WHERE id=?",
                (error, time.time() + delay, time.time(), event_id))

    def requeue(self, event_id=None, status="failed"):
        """Put failed (or dry_run) events back in the queue. Returns the number requeued."""
        now = time.time()
        if event_id is not None:
            cur = self._q("UPDATE events SET status='pending', attempts=0, next_attempt_at=0, updated_at=? "
                          "WHERE id=? AND status IN ('failed','dry_run','review','skipped')", (now, event_id))
        else:
            cur = self._q("UPDATE events SET status='pending', attempts=0, next_attempt_at=0, updated_at=? "
                          "WHERE status=?", (now, status))
        return cur.rowcount

    def list_events(self, status=None, limit=50):
        if status:
            return self._q("SELECT * FROM events WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
        return self._q("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def counts(self):
        return {r["status"]: r["n"] for r in self._q("SELECT status, COUNT(*) n FROM events GROUP BY status")}

    def purge(self, older_than_days):
        """Remove personal data from finished events older than N days. Keeps IDs and outcome."""
        cutoff = time.time() - older_than_days * 86400
        cur = self._q("UPDATE events SET record_json=NULL WHERE record_json IS NOT NULL AND updated_at<? "
                      "AND status IN ('done','skipped','dry_run')", (cutoff,))
        return cur.rowcount

    # ---- mappings -----------------------------------------------------
    def get_mapping(self, df_client_id):
        return self._q("SELECT * FROM mappings WHERE df_client_id=?", (df_client_id,)).fetchone()

    def mapping_for_contact(self, ghl_contact_id):
        return self._q("SELECT * FROM mappings WHERE ghl_contact_id=?", (ghl_contact_id,)).fetchone()

    def save_mapping(self, df_client_id, ghl_contact_id, fingerprint, confirmed=None):
        now = time.time()
        self._q("INSERT INTO mappings(df_client_id, ghl_contact_id, last_fingerprint, confirmed, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(df_client_id) DO UPDATE SET "
                "ghl_contact_id=excluded.ghl_contact_id, last_fingerprint=excluded.last_fingerprint, "
                "confirmed=MAX(mappings.confirmed, excluded.confirmed), updated_at=excluded.updated_at",
                (df_client_id, ghl_contact_id, fingerprint, 1 if confirmed else 0, now, now))

    def delete_mapping(self, df_client_id):
        return self._q("DELETE FROM mappings WHERE df_client_id=?", (df_client_id,)).rowcount

    def list_mappings(self, limit=100):
        return self._q("SELECT * FROM mappings ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()

    # ---- reviews ------------------------------------------------------
    def open_review(self, event_id, df_client_id, reason, candidates):
        with self._lock:
            existing = self._q("SELECT id FROM reviews WHERE df_client_id=? AND status='open' AND reason=?",
                               (df_client_id, reason)).fetchone()
            if existing:
                return existing["id"]
            cur = self._q("INSERT INTO reviews(event_id, df_client_id, reason, candidates, created_at) "
                          "VALUES (?,?,?,?,?)",
                          (event_id, df_client_id, reason, json.dumps(candidates), time.time()))
            return cur.lastrowid

    def has_open_review(self, df_client_id):
        return self._q("SELECT 1 FROM reviews WHERE df_client_id=? AND status='open'", (df_client_id,)).fetchone()

    def list_reviews(self, status="open"):
        return self._q("SELECT * FROM reviews WHERE status=? ORDER BY id", (status,)).fetchall()

    def get_review(self, review_id):
        return self._q("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()

    def close_review(self, review_id, resolution):
        self._q("UPDATE reviews SET status='resolved', resolution=?, resolved_at=? WHERE id=?",
                (resolution, time.time(), review_id))
