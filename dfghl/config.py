"""Settings loaded from environment variables (and an optional .env file).

Secrets are never printed. Use Config.redacted() when you need to show settings.
"""
import os
from dataclasses import dataclass, fields

SECRET_FIELDS = {"ghl_api_token", "webhook_secret"}


def load_dotenv(path=".env"):
    """Minimal .env loader. Real environment variables win over the file."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)


def _bool(name, default=False):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _str(name, default=""):
    value = os.environ.get(name, "").strip()
    return value or default


def _int(name, default):
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


@dataclass
class Config:
    ghl_api_token: str
    ghl_location_id: str
    webhook_secret: str
    ghl_base_url: str = "https://services.leadconnectorhq.com"
    ghl_api_version: str = "2021-07-28"
    sync_tag: str = "disputefox-synced"
    db_path: str = "data/dfghl.sqlite3"
    host: str = "0.0.0.0"
    port: int = 8080
    dry_run: bool = False
    test_client_id: str = ""
    test_email: str = ""
    default_country_code: str = "1"
    allow_email_as_client_id: bool = False
    max_attempts: int = 6
    rate_limit_per_10s: int = 80
    http_timeout: int = 20
    max_body_bytes: int = 65536
    ghl_client_id_field: str = ""

    @classmethod
    def from_env(cls, require_secrets=True):
        load_dotenv(os.environ.get("DFGHL_ENV_FILE", ".env"))
        cfg = cls(
            ghl_api_token=_str("GHL_API_TOKEN"),
            ghl_location_id=_str("GHL_LOCATION_ID"),
            webhook_secret=_str("WEBHOOK_SECRET"),
            ghl_base_url=_str("GHL_BASE_URL", cls.ghl_base_url).rstrip("/"),
            ghl_api_version=_str("GHL_API_VERSION", cls.ghl_api_version),
            sync_tag=_str("SYNC_TAG", cls.sync_tag),
            db_path=_str("DB_PATH", cls.db_path),
            host=_str("HOST", cls.host),
            port=_int("PORT", cls.port),
            dry_run=_bool("DRY_RUN"),
            test_client_id=_str("TEST_CLIENT_ID"),
            test_email=_str("TEST_EMAIL").lower(),
            default_country_code=_str("DEFAULT_COUNTRY_CODE", "1").lstrip("+"),
            allow_email_as_client_id=_bool("ALLOW_EMAIL_AS_CLIENT_ID"),
            max_attempts=_int("MAX_ATTEMPTS", cls.max_attempts),
            rate_limit_per_10s=_int("RATE_LIMIT_PER_10S", cls.rate_limit_per_10s),
            http_timeout=_int("HTTP_TIMEOUT", cls.http_timeout),
            max_body_bytes=_int("MAX_BODY_BYTES", cls.max_body_bytes),
            ghl_client_id_field=_str("GHL_CLIENT_ID_FIELD_ID"),
        )
        if require_secrets:
            missing = [n for n, v in (("GHL_API_TOKEN", cfg.ghl_api_token),
                                      ("GHL_LOCATION_ID", cfg.ghl_location_id),
                                      ("WEBHOOK_SECRET", cfg.webhook_secret)) if not v]
            if missing:
                raise SystemExit("Missing required environment variables: " + ", ".join(missing))
            if len(cfg.webhook_secret) < 24:
                raise SystemExit("WEBHOOK_SECRET must be at least 24 characters. "
                                 "Generate one with: python -c \"import secrets;print(secrets.token_urlsafe(32))\"")
        return cfg

    @property
    def test_mode(self):
        return bool(self.test_client_id or self.test_email)

    def redacted(self):
        out = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in SECRET_FIELDS:
                value = "***set***" if value else "(missing)"
            out[f.name] = value
        return out
