"""Shared settings for the Unraid plugin.

Resolution order is file, then environment, then built-in default. The
dashboard writes the file; environment variables remain the fallback so a
headless install needs no UI, and an existing env-configured install keeps
working unchanged after an upgrade.

A key is only considered set in the file when it is present and non-empty, so
clearing a field in the UI hands control back to the environment rather than
pinning an empty value.
"""

import json
import os
import threading
from typing import Any, Dict

SETTINGS_FILENAME = "unraid_settings.json"

# key -> (env var, default). Types are inferred from the default.
FIELDS = {
    "scopes": ("UNRAID_SCOPES", "*:READ_ANY"),
    "protected_containers": ("UNRAID_PROTECTED_CONTAINERS", ""),
    "alerts_enabled": ("UNRAID_ALERTS_ENABLED", True),
    "min_importance": ("UNRAID_ALERT_MIN_IMPORTANCE", "WARNING"),
    "cooldown_seconds": ("UNRAID_ALERT_COOLDOWN_SECONDS", 300),
    "max_per_hour": ("UNRAID_ALERT_MAX_PER_HOUR", 20),
    "outbound_enabled": ("UNRAID_OUTBOUND_ENABLED", True),
}

_IMPORTANCE = ("INFO", "WARNING", "ALERT")
_lock = threading.Lock()


def path() -> str:
    home = (os.environ.get("HERMES_HOME") or "").strip() or os.path.expanduser("~/.hermes")
    return os.path.join(home, SETTINGS_FILENAME)


def _read_file() -> Dict[str, Any]:
    try:
        p = path()
        if os.path.exists(p):
            with open(p) as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - a bad settings file must not break the plugin
        pass
    return {}


def _coerce(value: Any, default: Any) -> Any:
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in ("0", "false", "no", "off", "")
    if isinstance(default, int):
        try:
            return max(0, int(str(value).strip()))
        except (TypeError, ValueError):
            return default
    return str(value).strip()


def load() -> Dict[str, Any]:
    """Effective settings, with a `sources` map naming where each value came from."""
    stored = _read_file()
    out: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    for key, (env_var, default) in FIELDS.items():
        if key in stored and str(stored[key]).strip() != "":
            out[key] = _coerce(stored[key], default)
            sources[key] = "settings"
            continue
        raw = (os.environ.get(env_var) or "").strip()
        if raw:
            out[key] = _coerce(raw, default)
            sources[key] = "env"
            continue
        out[key] = default
        sources[key] = "default"
    if out["min_importance"].upper() not in _IMPORTANCE:
        out["min_importance"] = "WARNING"
        sources["min_importance"] = "default"
    else:
        out["min_importance"] = out["min_importance"].upper()
    out["_sources"] = sources
    out["_path"] = path()
    return out


def save(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Merge `patch` into the settings file. Unknown keys are ignored.

    A key set to None or "" is removed, which returns that setting to the
    environment or default rather than storing a blank.
    """
    with _lock:
        stored = _read_file()
        for key, value in (patch or {}).items():
            if key not in FIELDS:
                continue
            if value is None or (isinstance(value, str) and not value.strip()):
                stored.pop(key, None)
            else:
                stored[key] = _coerce(value, FIELDS[key][1])
        p = path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(stored, fh, indent=2, sort_keys=True)
        os.replace(tmp, p)
    return load()
