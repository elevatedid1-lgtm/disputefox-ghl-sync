"""Core sync: one queued DisputeFox event -> one GHL contact create/update + tag."""
import json
import logging

from .ghl import GHLError
from .normalize import ClientRecord, mask_email, mask_phone

log = logging.getLogger("dfghl.sync")


def backoff_seconds(attempts):
    """30s, 2m, 8m, 32m, then hourly."""
    return min(3600, 30 * (4 ** attempts))


class Syncer:
    def __init__(self, cfg, store, ghl):
        self.cfg = cfg
        self.store = store
        self.ghl = ghl

    def _is_test_client(self, rec):
        if self.cfg.test_client_id and rec.client_id == self.cfg.test_client_id:
            return True
        if self.cfg.test_email and rec.email == self.cfg.test_email:
            return True
        return False

    def _review(self, event_id, rec, reason, candidates):
        rid = self.store.open_review(event_id, rec.client_id, reason, candidates)
        self.store.finish(event_id, "review", action="needs_review", error=reason)
        log.warning("event %s client %s -> REVIEW #%s: %s", event_id, rec.client_id, rid, reason)
        return "review"

    def process(self, row):
        """Process one event row. Returns the resulting status."""
        event_id = row["id"]
        if not row["record_json"]:
            self.store.finish(event_id, "failed", error="record was purged; cannot process")
            return "failed"
        rec = ClientRecord(**json.loads(row["record_json"]))
        who = f"client {rec.client_id} ({mask_email(rec.email) or '-'} / {mask_phone(rec.phone) or '-'})"

        if self.cfg.test_mode and not self._is_test_client(rec):
            self.store.finish(event_id, "skipped", action="test_mode_skip")
            log.info("event %s %s skipped: single-client test mode is on", event_id, who)
            return "skipped"

        if self.store.has_open_review(rec.client_id):
            return self._review(event_id, rec, "earlier conflict for this client is still open", [])

        mapping = self.store.get_mapping(rec.client_id)
        if mapping and mapping["last_fingerprint"] == rec.fingerprint():
            self.store.finish(event_id, "done", action="no_change", ghl_contact_id=mapping["ghl_contact_id"])
            log.info("event %s %s: no change since last sync", event_id, who)
            return "done"

        try:
            return self._sync(event_id, rec, mapping, who)
        except GHLError as exc:
            attempts = row["attempts"] + 1
            if exc.retryable and attempts < self.cfg.max_attempts:
                delay = backoff_seconds(row["attempts"])
                self.store.retry_later(event_id, str(exc), delay)
                log.warning("event %s %s: %s; retry %d/%d in %ds", event_id, who, exc, attempts,
                            self.cfg.max_attempts, delay)
                return "pending"
            self.store.finish(event_id, "failed", error=str(exc))
            log.error("event %s %s FAILED: %s", event_id, who, exc)
            return "failed"
        except Exception as exc:  # never let one bad record stop the worker
            self.store.finish(event_id, "failed", error=f"unexpected {type(exc).__name__}")
            log.exception("event %s %s FAILED with unexpected error", event_id, who)
            return "failed"

    def _sync(self, event_id, rec, mapping, who):
        by_email = self.ghl.find_duplicate(email=rec.email) if rec.email else None
        by_phone = self.ghl.find_duplicate(phone=rec.phone) if rec.phone else None
        matches = {"email": by_email, "phone": by_phone}
        found = {cid for cid in (by_email, by_phone) if cid}

        if mapping:
            target = mapping["ghl_contact_id"]
            others = found - {target}
            if others and not mapping["confirmed"]:
                return self._review(event_id, rec,
                                    "linked GHL contact differs from the contact that already has this email/phone",
                                    {"linked": target, **matches})
            action = "update"
        elif len(found) > 1:
            return self._review(event_id, rec, "email and phone match two different GHL contacts", matches)
        elif found:
            target = found.pop()
            owner = self.store.mapping_for_contact(target)
            if owner and owner["df_client_id"] != rec.client_id:
                return self._review(event_id, rec, "matching GHL contact is already linked to another DisputeFox client",
                                    {"ghl_contact": target, "linked_df_client": owner["df_client_id"]})
            action = "link_update"
        else:
            target = None
            action = "create"

        if self.cfg.dry_run:
            self.store.finish(event_id, "dry_run", action=f"would_{action}", ghl_contact_id=target)
            log.info("event %s %s DRY RUN: would %s %s", event_id, who, action, target or "new contact")
            return "dry_run"

        fields = rec.ghl_fields(self.cfg.ghl_client_id_field)
        if action == "create":
            target = self.ghl.create_contact(fields)
            # Save the link right away so a failure below can never lead to a second create.
            self.store.save_mapping(rec.client_id, target, None)
        else:
            try:
                self.ghl.update_contact(target, fields)
            except GHLError as exc:
                if action == "update" and exc.status in (400, 404) and "not found" in str(exc).lower():
                    return self._review(event_id, rec, "linked GHL contact no longer exists", {"linked": target})
                raise
            self.store.save_mapping(rec.client_id, target, None)

        self.ghl.add_tags(target, [self.cfg.sync_tag])
        self.store.save_mapping(rec.client_id, target, rec.fingerprint())
        self.store.finish(event_id, "done", action=action, ghl_contact_id=target)
        log.info("event %s %s -> %s GHL contact %s", event_id, who, action, target)
        return "done"

    def run_due(self, limit=25):
        """Process everything currently due. Returns a status -> count dict."""
        results = {}
        while True:
            rows = self.store.claim_due(limit)
            if not rows:
                return results
            for row in rows:
                status = self.process(row)
                results[status] = results.get(status, 0) + 1
            if all(self.store.get_event(r["id"])["status"] == "pending" for r in rows):
                return results  # everything left is waiting on a retry timer
