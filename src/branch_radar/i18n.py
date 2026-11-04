"""Terminal-side translation.

The console keeps Chinese as the source language: it is the language the code
is written in, so a string with no entry falls through readable rather than
turning into a missing-key placeholder. English is a lookup table keyed by that
source string, the same shape the Web console uses, so one wording change moves
both together.
"""

from __future__ import annotations

import json
import os
from importlib import resources

DEFAULT_LOCALE = "zh-CN"
SUPPORTED_LOCALES = ("zh-CN", "en")

_locale = DEFAULT_LOCALE
_catalog: dict[str, str] | None = None


def normalize_locale(value: str | None) -> str | None:
    """Map a flag or environment value onto a supported locale.

    Accepts what people and shells actually write: ``en``, ``en_US.UTF-8``,
    ``zh``, ``zh_CN.UTF-8``. Anything else returns None so the caller can fall
    through to the next source rather than guessing.
    """
    if not value:
        return None
    text = value.strip().replace("_", "-").split(".", 1)[0].lower()
    if not text or text in {"c", "posix"}:
        return None
    if text.startswith("zh"):
        return "zh-CN"
    if text.startswith("en"):
        return "en"
    return None


def resolve_locale(explicit: str | None = None) -> str:
    """Pick a locale from the flag, then the environment, then the default."""
    candidates = [explicit, os.environ.get("BRANCH_RADAR_LANG")]
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        candidates.append(os.environ.get(name))
    for candidate in candidates:
        resolved = normalize_locale(candidate)
        if resolved:
            return resolved
    return DEFAULT_LOCALE


def set_locale(value: str | None) -> str:
    global _locale, _catalog
    _locale = resolve_locale(value)
    _catalog = None
    return _locale


def locale() -> str:
    return _locale


def _load(name: str) -> dict[str, str]:
    try:
        raw = (
            resources.files("branch_radar.resources")
            .joinpath("cli.%s.json" % name)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        key: value
        for key, value in payload.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def t(value: str) -> str:
    """Translate a source string, or return it unchanged."""
    global _catalog
    if _locale == DEFAULT_LOCALE or not isinstance(value, str) or not value:
        return value
    if _catalog is None:
        _catalog = _load(_locale)
    return _catalog.get(value, value)
