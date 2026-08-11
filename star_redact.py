#!/usr/bin/env python3
"""Centralised redaction for anything that may reach a log, a job event or an
API response.

Two layers, because secrets arrive in two shapes:

* free text (subprocess stdout, exception strings) — `redact_text`
* structured data (parsed JSON credential payloads) — `redact_obj`

The rule everywhere is fail-closed: a value that *looks* secret is masked even
when we are not certain it is one. Over-masking a log line is cheap; leaking a
refresh token is not.
"""

import re

MASK = "[redacted]"

# Keys whose *value* is always masked, matched case-insensitively against the
# whole key name split on non-alphanumerics (so "refresh_token", "refreshToken"
# and "REFRESH-TOKEN" all hit).
SECRET_KEY_WORDS = frozenset((
    "token", "secret", "password", "passwd", "credential", "credentials",
    "authorization", "auth", "key", "apikey", "privatekey", "cookie",
    "session", "signature", "assertion", "bearer", "code_verifier",
))

# Keys that trip the heuristic above but carry no secret. Two groups:
# harmless Google/OAuth metadata names, and our own deliberately masked hints
# (`*_masked` values are already one-way truncations produced by mask_tail).
SECRET_KEY_ALLOW = frozenset((
    "key_type", "keyfile", "public_key_url", "auth_uri", "auth_provider",
    "auth_provider_x509_cert_url", "keys", "key_id", "authorized",
    "key_mode", "key_file_mode", "token_masked", "access_key_masked",
    "client_id_masked", "client_email_masked", "token_type",
    "needs_authorisation", "authorization_url",
))

_WORD_SPLIT = re.compile(r"[^a-z0-9]+")

_TEXT_PATTERNS = (
    # Authorization / bearer headers in any casing.
    re.compile(r"(?i)\b(authorization|proxy-authorization)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{8,}"),
    re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/=]{8,}"),
    # key=value / "key": "value" for secret-ish key names.
    re.compile(
        r"(?i)([\"']?\b\w*(?:token|secret|password|passwd|api[_-]?key|"
        r"access[_-]?key|private[_-]?key|client[_-]?secret|credential)\w*"
        r"[\"']?\s*[:=]\s*)([\"']?)([^\s,;&\"'}\]]{4,})(\2)"
    ),
    # PEM blocks.
    re.compile(r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"),
    # Well-known token shapes.
    re.compile(r"\bya29\.[A-Za-z0-9._\-]{10,}"),          # Google access token
    re.compile(r"\b1//[A-Za-z0-9._\-]{10,}"),             # Google refresh token
    re.compile(r"\bEA[A-Za-z0-9]{40,}"),                  # Facebook graph token
    re.compile(r"\bsk-[A-Za-z0-9._\-]{16,}"),             # generic sk- API key
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),                   # AWS/R2 access key id
    # JWTs.
    re.compile(r"\beyJ[A-Za-z0-9._\-]{10,}\.[A-Za-z0-9._\-]{6,}\.[A-Za-z0-9._\-]{6,}"),
)

MAX_TEXT = 64 * 1024


def is_secret_key(name):
    """True when a mapping key names something that must never be echoed."""
    if not isinstance(name, str):
        return False
    lowered = name.strip().lower()
    if lowered in SECRET_KEY_ALLOW:
        return False
    words = [w for w in _WORD_SPLIT.split(lowered) if w]
    if not words:
        return False
    if any(w in SECRET_KEY_WORDS for w in words):
        return True
    # Compact spellings such as "apikey" / "accesstoken" survive the split.
    joined = "".join(words)
    return any(w in joined for w in ("token", "secret", "password", "apikey",
                                     "privatekey", "clientsecret"))


def redact_text(value, limit=MAX_TEXT):
    """Mask secret-looking spans in free text and bound its length."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    if len(value) > limit:
        value = value[:limit] + "\n[truncated]"
    for pattern in _TEXT_PATTERNS:
        if pattern.groups >= 3:
            value = pattern.sub(lambda m: m.group(1) + m.group(2) + MASK + m.group(4), value)
        else:
            value = pattern.sub(MASK, value)
    return value


def redact_obj(value, _depth=0):
    """Recursively mask secret values in parsed JSON-ish data."""
    if _depth > 12:
        return MASK
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if is_secret_key(key):
                out[key] = MASK if item is not None else None
            else:
                out[key] = redact_obj(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_obj(item, _depth + 1) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def mask_tail(value, keep=4):
    """A stable non-reversible hint that a secret exists, e.g. '****abcd'.

    Only ever applied to values the operator already supplied, and only to the
    last few characters so a shoulder-surfed screenshot stays useless.
    """
    if not isinstance(value, str) or not value:
        return None
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * 4 + value[-keep:]
