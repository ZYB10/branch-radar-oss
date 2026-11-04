from __future__ import annotations

import re

_URL_USERINFO = re.compile(
    r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^\s/@]+@", re.IGNORECASE
)
_BEARER = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(token|private[_-]?token|access[_-]?token|refresh[_-]?token|oauth[_-]?token|"
    r"password|passwd|secret|client[_-]?secret|api[_-]?key|access[_-]?key)\b"
    r"(\s*[:=]\s*)"
    r"([^\s,;]+)"
)
_JSON_SECRET = re.compile(
    r'''(?ix)
    (?P<prefix>["'](?:token|private[_-]?token|access[_-]?token|refresh[_-]?token|
      oauth[_-]?token|password|passwd|secret|client[_-]?secret|api[_-]?key|
      access[_-]?key)["']\s*:\s*)
    (?P<quote>["'])[^"']*(?P=quote)
    '''
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PROVIDER_TOKEN = re.compile(
    r"""(?x)
    (?<![A-Za-z0-9_])
    (?:
        (?:ghp_|gho_)[A-Za-z0-9]{20,}
        | github_pat_[A-Za-z0-9_]{20,}
        | glpat-[A-Za-z0-9_-]{16,}
        | xox[baprs]-[A-Za-z0-9-]{16,}
        | sk-[a-z]+-[A-Za-z0-9_-]{16,}
        | AKIA[0-9A-Z]{16}
    )
    (?![A-Za-z0-9_])
    """
)


def redact_sensitive_text(value: str) -> str:
    """Remove common credentials before persistence or model handoff."""
    redacted = _URL_USERINFO.sub(lambda match: match.group("scheme"), value)
    redacted = _BEARER.sub(lambda match: match.group(1) + "<redacted>", redacted)
    redacted = _PROVIDER_TOKEN.sub("<redacted-provider-token>", redacted)
    redacted = _JSON_SECRET.sub(
        lambda match: match.group("prefix") + '"<redacted>"', redacted
    )
    redacted = _SECRET_ASSIGNMENT.sub(
        lambda match: "%s%s<redacted>" % (match.group(1), match.group(2)),
        redacted,
    )
    redacted = _JWT.sub("<redacted-jwt>", redacted)
    return redacted
