"""Backend for the Unraid dashboard tab.

Mounted at /api/plugins/unraid/ by the Hermes dashboard.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import types
from pathlib import Path
from typing import Any, Dict

try:
    from fastapi import APIRouter, Body
except Exception:  # Allows import without the dashboard dependencies present.
    class APIRouter:  # type: ignore
        def get(self, *_a, **_k):
            return lambda fn: fn

        def post(self, *_a, **_k):
            return lambda fn: fn

    def Body(default=None, **_k):  # type: ignore
        return default


router = APIRouter()

# The plugin package sits one level up. Every Hermes plugin's dashboard tab is
# mounted this same way, sharing one Python process -- so a plain `import
# settings`/`import tools` (relying on sys.path order for isolation) collides
# with any other plugin's same-named file via the shared sys.modules cache.
# Whichever plugin happens to import the bare name "settings" first "wins" it
# process-wide; every later `import settings`, from ANY plugin, silently gets
# that same cached module back regardless of sys.path. Confirmed live: this is
# why the Unraid dashboard tab was reading NextDNS's settings.py.
#
# tools.py's own `from . import api_map, settings` falls back to the same bare
# import when there's no real package context (exactly the case here), so
# fixing only this file's imports isn't enough -- this plugin's own directory
# is loaded as a genuine (synthetic) package instead, under a globally-unique
# name, so every `from . import x` inside it resolves normally.
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_PKG_NAME = "hermes_unraid_plugin_pkg"

if _PKG_NAME not in sys.modules:
    _pkg = types.ModuleType(_PKG_NAME)
    _pkg.__path__ = [str(_PLUGIN_DIR)]
    sys.modules[_PKG_NAME] = _pkg

try:
    _settings = importlib.import_module(f"{_PKG_NAME}.settings")
except Exception:  # noqa: BLE001
    _settings = None

try:
    _tools = importlib.import_module(f"{_PKG_NAME}.tools")
except Exception:  # noqa: BLE001
    _tools = None


def _error(message: str) -> Dict[str, Any]:
    return {"ok": False, "error": message}


@router.get("/settings")
def get_settings() -> Dict[str, Any]:
    """Current effective settings, plus where each value came from."""
    if _settings is None:
        return _error("settings module unavailable")
    data = _settings.load()
    sources = data.pop("_sources", {})
    return {
        "ok": True,
        "settings": data,
        "sources": sources,
        "importance_options": ["INFO", "WARNING", "ALERT"],
    }


@router.post("/settings")
def post_settings(patch: Dict[str, Any] = Body(default=None)) -> Dict[str, Any]:
    """Merge a partial update. Empty values return a key to env/default."""
    if _settings is None:
        return _error("settings module unavailable")
    if not isinstance(patch, dict):
        return _error("expected a JSON object")
    try:
        data = _settings.save(patch)
    except Exception as e:  # noqa: BLE001
        return _error(f"{type(e).__name__}: {e}")
    sources = data.pop("_sources", {})
    return {"ok": True, "settings": data, "sources": sources}


@router.get("/status")
def get_status() -> Dict[str, Any]:
    """Which tools are live, the API key's roles, and gateway platform state."""
    out: Dict[str, Any] = {"ok": True}
    if _tools is not None:
        try:
            out["permissions"] = _tools.permission_report()
        except Exception as e:  # noqa: BLE001
            out["permissions_error"] = f"{type(e).__name__}: {e}"
    home = (os.environ.get("HERMES_HOME") or "").strip() or os.path.expanduser("~/.hermes")
    try:
        with open(Path(home) / "gateway_state.json") as fh:
            state = json.load(fh)
        out["platform_state"] = (state.get("platforms") or {}).get("unraid")
    except Exception:  # noqa: BLE001 - absent before the gateway first writes it
        out["platform_state"] = None
    return out


@router.get("/capabilities")
def get_capabilities() -> Dict[str, Any]:
    """API fields and whether each is in scope, for the permissions view."""
    if _tools is None:
        return _error("tools module unavailable")
    try:
        return {"ok": True, **json.loads(_tools.unraid_api_capabilities({}))}
    except Exception as e:  # noqa: BLE001
        return _error(f"{type(e).__name__}: {e}")
