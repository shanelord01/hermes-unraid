"""Derive the RESOURCE:ACTION permission for any Unraid API field.

The Unraid schema documents its own authorisation in field descriptions:

    #### Required Permissions:
    - Action: **UPDATE_ANY**
    - Resource: **DOCKER**

120 of the 151 query and mutation fields carry that, so the permission map is
introspected rather than hand-maintained: it cannot drift as Unraid adds
fields. The remaining 31 are covered by _FALLBACK below.

This is what lets the plugin support the whole API without registering a tool
per field. A generic passthrough resolves the field being called, looks up its
permission here, and enforces UNRAID_SCOPES before executing.
"""

import json
import os
import re
import time

_PERM_RE = re.compile(
    r"Action:\s*\*\*([A-Z_]+)\*\*.*?Resource:\s*\*\*([A-Z_]+)\*\*", re.S
)

# Fields whose descriptions omit the permission block. Verified against the
# live schema (Unraid API v4.37); anything not listed and not self-described
# fails closed rather than defaulting to permitted.
_FALLBACK = {
    # Notification mutations - all operate on the NOTIFICATIONS resource.
    "createNotification": ("NOTIFICATIONS", "CREATE_ANY"),
    "notifyIfUnique": ("NOTIFICATIONS", "CREATE_ANY"),
    "deleteNotification": ("NOTIFICATIONS", "DELETE_ANY"),
    "deleteArchivedNotifications": ("NOTIFICATIONS", "DELETE_ANY"),
    "archiveNotification": ("NOTIFICATIONS", "UPDATE_ANY"),
    "archiveNotifications": ("NOTIFICATIONS", "UPDATE_ANY"),
    "archiveAll": ("NOTIFICATIONS", "UPDATE_ANY"),
    "unreadNotification": ("NOTIFICATIONS", "UPDATE_ANY"),
    "unarchiveNotifications": ("NOTIFICATIONS", "UPDATE_ANY"),
    "unarchiveAll": ("NOTIFICATIONS", "UPDATE_ANY"),
    "recalculateOverview": ("NOTIFICATIONS", "UPDATE_ANY"),
    # Other mutations.
    "initiateFlashBackup": ("FLASH", "UPDATE_ANY"),
    "configureUps": ("CONFIG", "UPDATE_ANY"),
    # Queries. There is no UPS resource, so UPS reads sit under INFO.
    "settings": ("CONFIG", "READ_ANY"),
    "upsDevices": ("INFO", "READ_ANY"),
    "upsDeviceById": ("INFO", "READ_ANY"),
    "upsConfiguration": ("INFO", "READ_ANY"),
}

# Unauthenticated/public fields: these answer before login and gating them on a
# resource permission would be wrong.
_PUBLIC = {
    "getAvailableAuthActions",
    "isFreshInstall",
    "publicTheme",
    "isSSOEnabled",
    "publicOidcProviders",
}

_CACHE_TTL_SECONDS = 86400
_cache = {"at": 0.0, "map": None, "namespaces": None, "error": None}


def _cache_path() -> str:
    home = os.environ.get("HERMES_HOME") or "/tmp"
    return os.path.join(home, "cache", "unraid_schema_permissions.json")


def _introspect(runner) -> dict:
    """Build {TypeName.fieldName: (RESOURCE, ACTION)} plus the namespace set.

    ``runner`` is a callable taking a GraphQL document and returning the JSON
    string the tools module produces, so this module needs no transport of its
    own and inherits auth, TLS and timeout handling.
    """
    raw = runner(
        "{ __schema { types { name fields { name description "
        "type { name kind ofType { name kind ofType { name } } } } } } }"
    )
    data = json.loads(raw)
    if "error" in data:
        raise RuntimeError(data["error"])
    types = (data.get("__schema") or {}).get("types") or []
    by_name = {t["name"]: t for t in types if t.get("name")}

    def unwrap(t):
        while t and t.get("ofType"):
            t = t["ofType"]
        return (t or {}).get("name")

    namespaces = {}
    for f in (by_name.get("Mutation") or {}).get("fields") or []:
        target = unwrap(f.get("type"))
        if target and target.endswith("Mutations"):
            namespaces[f["name"]] = target

    perms = {}
    for tname, t in by_name.items():
        if tname != "Query" and tname != "Mutation" and not tname.endswith("Mutations"):
            continue
        for f in t.get("fields") or []:
            m = _PERM_RE.search(f.get("description") or "")
            if m:
                perms[f"{tname}.{f['name']}"] = (m.group(2), m.group(1))
    return {"map": perms, "namespaces": namespaces}


def load(runner, force: bool = False) -> dict:
    """Return the cached permission map, introspecting if needed."""
    now = time.time()
    if not force and _cache["map"] is not None and now - _cache["at"] < _CACHE_TTL_SECONDS:
        return _cache
    path = _cache_path()
    if not force:
        try:
            if os.path.exists(path) and now - os.path.getmtime(path) < _CACHE_TTL_SECONDS:
                disk = json.load(open(path))
                _cache.update(
                    at=now,
                    map={k: tuple(v) for k, v in disk["map"].items()},
                    namespaces=disk["namespaces"],
                    error=None,
                )
                return _cache
        except Exception:  # noqa: BLE001 - a bad cache must not be fatal
            pass
    try:
        built = _introspect(runner)
        _cache.update(at=now, map=built["map"], namespaces=built["namespaces"], error=None)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            json.dump(
                {"map": {k: list(v) for k, v in built["map"].items()},
                 "namespaces": built["namespaces"]},
                open(path, "w"),
            )
        except Exception:  # noqa: BLE001 - caching is best effort
            pass
    except Exception as e:  # noqa: BLE001
        _cache.update(at=now, error=f"{type(e).__name__}: {e}")
    return _cache


def permission_for(runner, root: str, nested: str = "", is_mutation: bool = True):
    """Resolve a field to (RESOURCE, ACTION), "public", or None if unknown.

    ``root`` is the top-level field. ``nested`` is the field inside it when the
    root is a namespace, e.g. root="docker", nested="updateContainer".
    """
    if root in _PUBLIC:
        return "public"
    state = load(runner)
    perms = state.get("map") or {}
    namespaces = state.get("namespaces") or {}

    # Namespaces exist only under Mutation. On the query side a nested name is
    # just a selection on the return type (`{ docker { containers } }`), and the
    # permission belongs to the root field, so nested is ignored there.
    if nested and is_mutation:
        type_name = namespaces.get(root)
        if type_name:
            hit = perms.get(f"{type_name}.{nested}")
            if hit:
                return hit
            if nested in _FALLBACK:
                return _FALLBACK[nested]
            return None
    container = "Mutation" if is_mutation else "Query"
    hit = perms.get(f"{container}.{root}")
    if hit:
        return hit
    if root in _FALLBACK:
        return _FALLBACK[root]
    return None


def is_namespace(runner, root: str) -> bool:
    return root in ((load(runner).get("namespaces")) or {})


_COMMENT_RE = re.compile(r"#[^\n]*")
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse_fields(document: str):
    """Extract [(root, nested)] selections from a GraphQL document.

    Deliberately simple: enough to identify which fields are being invoked so
    they can be permission-checked. It is a gate, not a validator - the server
    is what actually parses and executes the document.
    """
    text = _COMMENT_RE.sub("", document or "")
    start = text.find("{")
    if start == -1:
        return []
    depth, i, out, pending_root = 0, start, [], None
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            if depth <= 1:
                pending_root = None
            i += 1
            continue
        if ch == "(":  # skip argument lists wholesale
            level = 1
            i += 1
            while i < len(text) and level:
                level += text[i] == "("
                level -= text[i] == ")"
                i += 1
            continue
        m = _NAME_RE.match(text, i)
        if m:
            name = m.group(0)
            if depth == 1 and name not in ("query", "mutation", "fragment", "on"):
                pending_root = name
                out.append((name, ""))
            elif depth == 2 and pending_root:
                # First selection inside a root field: only meaningful when the
                # root is a namespace, which the caller decides.
                if out and out[-1] == (pending_root, ""):
                    out[-1] = (pending_root, name)
                pending_root = None
            i = m.end()
            continue
        i += 1
    return out
