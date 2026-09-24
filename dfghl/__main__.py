"""Command line: python -m dfghl <command>. Run `python -m dfghl -h` for the list."""
import argparse
import datetime as dt
import json
import logging
import sys
import urllib.error
import urllib.request

from .config import Config
from .ghl import GHLClient, GHLError
from .store import Store
from .sync import Syncer


class RedactFilter(logging.Filter):
    """Belt and braces: scrub secrets from any log line."""

    def __init__(self, secrets):
        super().__init__()
        self.secrets = [s for s in secrets if s]

    def filter(self, record):
        msg = record.getMessage()
        if any(s in msg for s in self.secrets):
            for s in self.secrets:
                msg = msg.replace(s, "***")
            record.msg, record.args = msg, ()
        return True


def setup_logging(cfg):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactFilter([cfg.ghl_api_token, cfg.webhook_secret]))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


def ts(value):
    return dt.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M") if value else "-"


def build(require_secrets=True):
    cfg = Config.from_env(require_secrets=require_secrets)
    setup_logging(cfg)
    store = Store(cfg.db_path)
    return cfg, store, Syncer(cfg, store, GHLClient(cfg))


def cmd_serve(args):
    from .server import serve
    cfg, store, syncer = build()
    serve(cfg, store, syncer)


def cmd_check(args):
    cfg, store, syncer = build()
    print("Settings:", json.dumps(cfg.redacted(), indent=2))
    try:
        syncer.ghl.find_duplicate(email="dfghl-connection-check@example.invalid")
        print("GHL connection OK (read-only duplicate search succeeded).")
    except GHLError as exc:
        hint = ""
        if exc.status == 401:
            hint = " -> token is wrong/expired, or missing scopes contacts.readonly + contacts.write"
        elif exc.status in (400, 403):
            hint = " -> check GHL_LOCATION_ID matches the sub-account the token belongs to"
        print(f"GHL connection FAILED: {exc}{hint}")
        sys.exit(1)


def cmd_process(args):
    cfg, store, syncer = build()
    print(json.dumps(syncer.run_due() or {"nothing": "due"}))


def cmd_status(args):
    cfg, store, _ = build(require_secrets=False)
    print("Mode:", "DRY RUN" if cfg.dry_run else "LIVE", "| single-client test mode" if cfg.test_mode else "")
    print("Events by status:", json.dumps(store.counts()))
    print("Linked clients:", len(store.list_mappings(limit=10**9)))
    print("Open reviews:", len(store.list_reviews("open")))


def cmd_events(args):
    _, store, _ = build(require_secrets=False)
    for r in store.list_events(args.status, args.limit):
        print(f"#{r['id']:<6} {ts(r['received_at'])}  client={r['client_id']:<14} status={r['status']:<8} "
              f"action={r['action'] or '-':<14} ghl={r['ghl_contact_id'] or '-':<22} tries={r['attempts']} "
              f"{('error=' + r['last_error']) if r['last_error'] else ''}")


def cmd_retry(args):
    _, store, _ = build(require_secrets=False)
    if args.id:
        n = store.requeue(event_id=args.id)
    else:
        n = store.requeue(status=args.status)
    print(f"Requeued {n} event(s). The running server picks them up within ~15s, or run: python -m dfghl process")


def cmd_reviews(args):
    _, store, _ = build(require_secrets=False)
    rows = store.list_reviews("resolved" if args.resolved else "open")
    if not rows:
        print("No reviews.")
    for r in rows:
        print(f"review #{r['id']}  event #{r['event_id']}  client={r['df_client_id']}  {ts(r['created_at'])}\n"
              f"   reason: {r['reason']}\n   candidates: {r['candidates']}"
              + (f"\n   resolution: {r['resolution']}" if r["resolution"] else ""))


def cmd_resolve(args):
    _, store, _ = build(require_secrets=False)
    review = store.get_review(args.review_id)
    if not review or review["status"] != "open":
        sys.exit("No open review with that id.")
    client_id = review["df_client_id"]
    if args.link:
        owner = store.mapping_for_contact(args.link)
        if owner and owner["df_client_id"] != client_id:
            sys.exit(f"GHL contact {args.link} is already linked to DisputeFox client {owner['df_client_id']}. "
                     f"Resolve that first (unlink it with: python -m dfghl unlink {owner['df_client_id']}).")
        store.save_mapping(client_id, args.link, None, confirmed=True)
        resolution = f"linked to GHL contact {args.link} by hand"
    else:
        resolution = "dismissed; client not synced"
    # Close every open review for this client, then requeue its latest event if linking.
    for r in store.list_reviews("open"):
        if r["df_client_id"] == client_id:
            store.close_review(r["id"], resolution)
    if args.link:
        store.requeue(event_id=review["event_id"])
        print(f"Linked. Event #{review['event_id']} requeued; it will update contact {args.link} and add the tag.")
    else:
        print("Dismissed. Future DisputeFox updates for this client will be processed again.")


def cmd_unlink(args):
    _, store, _ = build(require_secrets=False)
    print("Removed." if store.delete_mapping(args.client_id) else "No link for that client.")


def cmd_mappings(args):
    _, store, _ = build(require_secrets=False)
    for m in store.list_mappings(args.limit):
        print(f"DisputeFox {m['df_client_id']:<16} -> GHL {m['ghl_contact_id']:<24} "
              f"{'(confirmed by hand)' if m['confirmed'] else ''} updated {ts(m['updated_at'])}")


def cmd_send_test(args):
    """POST a sample payload to a running receiver, exactly like DisputeFox would."""
    cfg = Config.from_env(require_secrets=False)
    if not cfg.webhook_secret:
        sys.exit("WEBHOOK_SECRET is not set.")
    payload = {"client_id": args.client_id, "first_name": args.first_name, "last_name": args.last_name,
               "email": args.email, "phone": args.phone}
    req = urllib.request.Request(args.url.rstrip("/") + "/webhooks/disputefox",
                                 data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Webhook-Token": cfg.webhook_secret})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            print(resp.status, resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(exc.code, exc.read().decode())
        sys.exit(1)


def cmd_purge(args):
    _, store, _ = build(require_secrets=False)
    print(f"Cleared personal data from {store.purge(args.days)} finished event(s) older than {args.days} days.")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m dfghl", description="DisputeFox -> GoHighLevel contact sync")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the webhook receiver + worker").set_defaults(fn=cmd_serve)
    sub.add_parser("check", help="verify settings and GHL access (read-only)").set_defaults(fn=cmd_check)
    sub.add_parser("process", help="process due events once, then exit").set_defaults(fn=cmd_process)
    sub.add_parser("status", help="queue and review summary").set_defaults(fn=cmd_status)

    e = sub.add_parser("events", help="list recent events")
    e.add_argument("--status", choices=["pending", "done", "skipped", "review", "failed", "dry_run"])
    e.add_argument("--limit", type=int, default=50)
    e.set_defaults(fn=cmd_events)

    r = sub.add_parser("retry", help="requeue failed events (or one event)")
    r.add_argument("--id", type=int)
    r.add_argument("--status", default="failed", choices=["failed", "dry_run", "skipped"])
    r.set_defaults(fn=cmd_retry)

    rv = sub.add_parser("reviews", help="list conflicts waiting for a person")
    rv.add_argument("--resolved", action="store_true")
    rv.set_defaults(fn=cmd_reviews)

    rs = sub.add_parser("resolve", help="resolve a review")
    rs.add_argument("review_id", type=int)
    g = rs.add_mutually_exclusive_group(required=True)
    g.add_argument("--link", metavar="GHL_CONTACT_ID", help="link the client to this GHL contact")
    g.add_argument("--dismiss", action="store_true", help="close without syncing")
    rs.set_defaults(fn=cmd_resolve)

    u = sub.add_parser("unlink", help="remove the link for one DisputeFox client (does not touch GHL)")
    u.add_argument("client_id")
    u.set_defaults(fn=cmd_unlink)

    m = sub.add_parser("mappings", help="list DisputeFox -> GHL links")
    m.add_argument("--limit", type=int, default=100)
    m.set_defaults(fn=cmd_mappings)

    t = sub.add_parser("send-test", help="POST one fake client to a running receiver")
    t.add_argument("--url", default="http://127.0.0.1:8080")
    t.add_argument("--client-id", default="TEST-0001")
    t.add_argument("--first-name", default="Test")
    t.add_argument("--last-name", default="Client")
    t.add_argument("--email", required=True)
    t.add_argument("--phone", default="")
    t.set_defaults(fn=cmd_send_test)

    pg = sub.add_parser("purge", help="clear names/emails/phones from old finished events")
    pg.add_argument("--days", type=int, default=30)
    pg.set_defaults(fn=cmd_purge)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
