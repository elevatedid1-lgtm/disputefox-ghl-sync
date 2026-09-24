"""Turn whatever DisputeFox's AutoFox API Action sends into a clean client record.

DisputeFox does not publish the exact payload format of the AutoFox API Action,
so this accepts JSON or form-encoded bodies, flattens nested objects
(e.g. {"client": {...}}) and matches field names through a list of aliases.
Any alias can be overridden with FIELD_<NAME> environment variables, e.g.
FIELD_CLIENT_ID=customer_number.

Only the five fields below are ever extracted. Everything else in the body
(including anything sensitive someone adds to the AutoFox template later) is
discarded and never stored.
"""
import hashlib
import json
import os
import re
from dataclasses import dataclass
from urllib.parse import parse_qs

ALIASES = {
    "client_id": ["clientid", "client_id", "dfclientid", "disputefoxclientid", "customerid", "customer_id",
                  "clientnumber", "clientno", "id"],
    "first_name": ["firstname", "first_name", "fname", "clientfirstname", "givenname"],
    "last_name": ["lastname", "last_name", "lname", "clientlastname", "surname", "familyname"],
    "email": ["email", "emailaddress", "clientemail", "email1", "primaryemail"],
    "phone": ["phone", "mobile", "mobilephone", "cellphone", "cell", "phonenumber", "clientphone",
              "mobilenumber", "homephone", "primaryphone"],
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Unfilled merge fields such as "{{client.email}}", "[email]" or "%email%".
PLACEHOLDER_RE = re.compile(r"^(\{\{.*\}\}|\{.*\}|\[.*\]|%.*%|#.*#)$")


class PayloadError(ValueError):
    pass


@dataclass
class ClientRecord:
    client_id: str
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""

    def ghl_fields(self):
        """Fields to send to GHL. Empty values are left out so we never blank out data."""
        out = {}
        if self.first_name:
            out["firstName"] = self.first_name
        if self.last_name:
            out["lastName"] = self.last_name
        if self.email:
            out["email"] = self.email
        if self.phone:
            out["phone"] = self.phone
        return out

    def fingerprint(self):
        raw = json.dumps([self.client_id, self.first_name, self.last_name, self.email, self.phone])
        return hashlib.sha256(raw.encode()).hexdigest()


def _key(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def parse_body(raw: bytes, content_type: str):
    """Return a dict from a JSON or form-encoded body."""
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    ctype = (content_type or "").lower()
    if "json" in ctype or text[:1] in "{[":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PayloadError(f"Body is not valid JSON: {exc.msg}") from None
        if isinstance(data, list):
            if len(data) != 1 or not isinstance(data[0], dict):
                raise PayloadError("Expected one JSON object per request")
            data = data[0]
        if not isinstance(data, dict):
            raise PayloadError("Expected a JSON object")
        return data
    parsed = parse_qs(text, keep_blank_values=True)
    if not parsed:
        raise PayloadError("Body is neither JSON nor form-encoded")
    return {k: v[-1] for k, v in parsed.items()}


def flatten(data, prefix=""):
    """{"client": {"email": x}} -> {"client.email": x, "email": x} (shallowest key wins)."""
    flat = {}
    nested = []
    for k, v in data.items():
        if isinstance(v, dict):
            nested.append((k, v))
        elif not isinstance(v, list):
            flat.setdefault(f"{prefix}{k}", v)
    for k, v in nested:
        for nk, nv in flatten(v, f"{prefix}{k}.").items():
            flat.setdefault(nk, nv)
            flat.setdefault(nk.rsplit(".", 1)[-1], nv)
    return flat


def _clean(value):
    if value is None:
        return ""
    value = str(value).strip()
    if PLACEHOLDER_RE.match(value):
        return ""
    return value


def normalize_phone(value, default_cc="1"):
    """Return E.164 (+15551234567) or "" if it cannot be read as a phone number."""
    value = _clean(value)
    if not value:
        return ""
    has_plus = value.startswith("+")
    digits = re.sub(r"\D", "", value)
    if has_plus:
        return f"+{digits}" if 8 <= len(digits) <= 15 else ""
    if default_cc == "1":
        if len(digits) == 10:
            return f"+1{digits}"
        if len(digits) == 11 and digits.startswith("1"):
            return f"+{digits}"
        return ""
    if 6 <= len(digits) <= 12:
        return f"+{default_cc}{digits.lstrip('0')}"
    return ""


def normalize_email(value):
    value = _clean(value).lower()
    return value if EMAIL_RE.match(value) else ""


def extract(data: dict, default_cc="1", allow_email_as_client_id=False):
    """Build a ClientRecord from a parsed payload. Returns (record, notes)."""
    flat = flatten(data)
    by_key = {}
    for k, v in flat.items():
        by_key.setdefault(_key(k), v)
        by_key.setdefault(_key(k.rsplit(".", 1)[-1]), v)

    def pick(field):
        override = os.environ.get(f"FIELD_{field.upper()}")
        names = [override] if override else ALIASES[field]
        for name in names:
            if _key(name) in by_key:
                value = _clean(by_key[_key(name)])
                if value:
                    return value
        return ""

    notes = []
    raw_email = pick("email")
    raw_phone = pick("phone")
    email = normalize_email(raw_email)
    phone = normalize_phone(raw_phone, default_cc)
    if raw_email and not email:
        notes.append("email ignored: not a valid address")
    if raw_phone and not phone:
        notes.append("phone ignored: could not convert to E.164")

    client_id = pick("client_id")
    if not client_id and allow_email_as_client_id and email:
        client_id = f"email:{email}"
        notes.append("no client id in payload; using email as the key")
    if not client_id:
        raise PayloadError("No DisputeFox client ID in payload. Add the client ID merge field to the "
                           "AutoFox API Action body (see README), or set FIELD_CLIENT_ID.")
    if not email and not phone:
        raise PayloadError("Payload has neither a valid email nor a valid phone; cannot match or create.")

    record = ClientRecord(
        client_id=client_id,
        first_name=pick("first_name")[:100],
        last_name=pick("last_name")[:100],
        email=email,
        phone=phone,
    )
    return record, notes


def mask_email(email):
    if not email or "@" not in email:
        return email or ""
    user, _, domain = email.partition("@")
    return f"{user[:1]}***@{domain}"


def mask_phone(phone):
    return f"***{phone[-4:]}" if phone else ""
