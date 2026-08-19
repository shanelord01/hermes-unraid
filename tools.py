"""Tool handlers for the Unraid plugin.

Contract per Hermes plugin rules: handlers accept (args: dict, **kwargs),
always return a JSON string, and never raise.

Every tool declares a RESOURCE:ACTION permission using Unraid's own grammar.
UNRAID_SCOPES decides which are registered; it defaults to *:READ_ANY, so the
plugin is read-only unless actuation is granted explicitly. See
__init__.register() for the registration table.
"""

import json
import os
import re
import socket
import ssl
import urllib.error
import urllib.request

try:  # package import when loaded as a Hermes plugin
    from . import api_map, settings as _settings_mod
except ImportError:  # direct import when testing the module standalone
    import api_map
    import settings as _settings_mod

_TIMEOUT_SECONDS = 15

# refreshDockerDigests contacts every container's registry serially, so it
# scales with container count and routinely exceeds a minute on a busy host.
# The default timeout would abort it client-side while the server carried on,
# leaving digests half-populated and update status mostly "undetermined".
_REFRESH_TIMEOUT_SECONDS = 240

# Container updates pull images and recreate containers; lifecycle actions can
# block on a slow stop. Both outlive the default timeout on a busy host.
_MUTATION_TIMEOUT_SECONDS = 240

# ---------------------------------------------------------------------------
# Scopes - RESOURCE:ACTION, mirroring Unraid's own permission grammar
# ---------------------------------------------------------------------------
# Unraid's API keys are described as RESOURCE:ACTION pairs (DOCKER:READ_ANY,
# NOTIFICATIONS:UPDATE_ANY, ...) across 29 resources and 8 actions. We use the
# same vocabulary rather than inventing one, so what you write in
# UNRAID_SCOPES matches what you granted the key with `unraid-api apikey`.
#
#   UNRAID_SCOPES="DOCKER:READ_ANY,DOCKER:UPDATE_ANY,LOGS:READ_ANY"
#   UNRAID_SCOPES="DOCKER:*,LOGS:READ_ANY"   # every action on DOCKER
#   UNRAID_SCOPES="*:READ_ANY"               # everything, read-only (default)
#
# Unset defaults to *:READ_ANY, which is the behaviour this plugin shipped
# with before scopes existed: all read tools, no actuation.
#
# This is the client-side half only. The key's own permissions are enforced by
# the server regardless, so a VIEWER key still refuses mutations even if a
# scope is set here. What this layer adds is intent (the operator says what
# the agent may attempt), tool-surface control (unscoped tools are never
# registered, so the model cannot plan around them), and the protected
# container rules further down, which the server has no concept of.

_DEFAULT_SCOPES = "*:READ_ANY"

READ = "READ_ANY"
UPDATE = "UPDATE_ANY"

# Tool -> the single permission it requires. Aggregate tools such as
# unraid_overview touch several resources; they declare their headline one and
# return whatever the key is allowed to see, since GraphQL answers partially.
TOOL_PERMISSIONS = {
    "unraid_overview": ("ARRAY", READ),
    "unraid_disks": ("DISK", READ),
    "unraid_containers": ("DOCKER", READ),
    "unraid_notifications": ("NOTIFICATIONS", READ),
    "unraid_parity": ("ARRAY", READ),
    "unraid_shares": ("SHARE", READ),
    "unraid_metrics": ("INFO", READ),
    "unraid_vms": ("VMS", READ),
    "unraid_logs": ("LOGS", READ),
    "unraid_container_logs": ("LOGS", READ),
    "unraid_graphql": ("INFO", READ),
    "unraid_check_updates": ("DOCKER", UPDATE),
    "unraid_update_containers": ("DOCKER", UPDATE),
    "unraid_container_power": ("DOCKER", UPDATE),
    "unraid_install_plugin": ("CONFIG", UPDATE),
    "unraid_notification_manage": ("NOTIFICATIONS", UPDATE),
}

# Meta tools: always registered. unraid_api_capabilities only reads the
# schema, and unraid_api enforces permissions per field at call time, so
# neither can be gated on a single RESOURCE:ACTION.
ALWAYS_ON_TOOLS = ("unraid_permissions", "unraid_api_capabilities", "unraid_api")


def parse_scopes() -> set:
    """Configured scopes as a set of (RESOURCE, ACTION), upper-cased.

    A bare resource ("DOCKER") means every action on it. Unknown or malformed
    entries are ignored rather than raising - a typo should cost you a tool,
    not the whole plugin.
    """
    raw = str(_settings_mod.load().get("scopes") or "").strip() or _DEFAULT_SCOPES
    out = set()
    for item in raw.split(","):
        item = item.strip().upper()
        if not item:
            continue
        if ":" in item:
            resource, _, action = item.partition(":")
            resource, action = resource.strip(), action.strip()
        else:
            resource, action = item, "*"
        if resource:
            out.add((resource, action or "*"))
    return out


def has_scope(resource: str, action: str) -> bool:
    resource, action = resource.upper(), action.upper()
    for scope_resource, scope_action in parse_scopes():
        if scope_resource in ("*", resource) and scope_action in ("*", action):
            return True
    return False


def tool_allowed(tool_name: str) -> bool:
    perm = TOOL_PERMISSIONS.get(tool_name)
    if not perm:
        return False
    return has_scope(*perm)


def _require(tool_name: str):
    """Return an error JSON string if the tool is out of scope, else None.

    Tools are only registered when in scope, so this guards against a stale
    registration or a direct call.
    """
    if tool_allowed(tool_name):
        return None
    resource, action = TOOL_PERMISSIONS.get(tool_name, ("?", "?"))
    return json.dumps(
        {
            "error": f"{tool_name} requires scope {resource}:{action}, which is not configured.",
            "hint": "Add it to UNRAID_SCOPES, and grant the same permission on the API key.",
        }
    )


def key_permissions() -> dict:
    """The API key's effective permissions, as reported by the server.

    Effective access is the union of two things: the permissions granted
    explicitly on the key, and those implied by its roles. me.permissions only
    returns the explicit half, so reading it alone under-reports - a VIEWER key
    with no explicit LOGS grant can still read logs, because the role carries
    LOGS:READ_ANY. Reporting that as "blocked" would be wrong, so both halves
    are merged here.

    Returns {"name", "roles", "permissions": {RESOURCE: [ACTION, ...]},
    "explicit": {...}} or {"error": ...} if it cannot be determined.
    """
    raw = _run("{ me { name roles permissions { resource actions } } }")
    try:
        data = json.loads(raw)
        if "error" in data:
            return {"error": data["error"]}
        me = data.get("me", {}) or {}
        explicit = {}
        for p in me.get("permissions") or []:
            resource = (p.get("resource") or "").upper()
            if resource:
                explicit.setdefault(resource, set()).update(
                    a.upper() for a in (p.get("actions") or [])
                )
        roles = me.get("roles") or []
        effective = {r: set(a) for r, a in explicit.items()}
        if roles:
            role_list = ", ".join(str(r).upper() for r in roles)
            role_raw = _run(
                "{ getPermissionsForRoles(roles: [%s]) { resource actions } }" % role_list
            )
            role_data = json.loads(role_raw)
            if "error" not in role_data:
                for p in role_data.get("getPermissionsForRoles") or []:
                    resource = (p.get("resource") or "").upper()
                    if resource:
                        effective.setdefault(resource, set()).update(
                            a.upper() for a in (p.get("actions") or [])
                        )
        return {
            "name": me.get("name"),
            "roles": roles,
            "permissions": {r: sorted(a) for r, a in effective.items()},
            "explicit": {r: sorted(a) for r, a in explicit.items()},
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def permission_report() -> dict:
    """Reconcile configured scopes against the key's actual permissions.

    Answers the question an operator actually has: which tools are live, and
    for the ones that are not, is it my config or my API key?
    """
    key = key_permissions()
    key_perms = key.get("permissions") or {}
    key_known = "error" not in key
    live, blocked_by_key, not_configured = [], [], []
    for tool, (resource, action) in sorted(TOOL_PERMISSIONS.items()):
        if not has_scope(resource, action):
            not_configured.append(f"{tool} ({resource}:{action})")
            continue
        granted = key_perms.get(resource, [])
        if key_known and action not in granted:
            blocked_by_key.append(
                f"{tool} needs {resource}:{action}; key's effective permissions for "
                f"{resource} are {granted or 'none'}"
            )
        else:
            live.append(tool)
    report = {
        "configured_scopes": sorted(f"{r}:{a}" for r, a in parse_scopes()),
        "api_key_name": key.get("name"),
        "api_key_roles": key.get("roles"),
        "tools_registered": live,
        "tools_likely_blocked_by_api_key": blocked_by_key,
        "tools_not_in_scope": not_configured,
    }
    if blocked_by_key:
        # Advisory, not authoritative: the server is the only thing that
        # decides. Registration deliberately keys off scopes alone so a
        # mis-read permission cannot make a working tool disappear.
        report["note_on_blocked"] = (
            "These are in scope but the API key appears to lack the permission. They are "
            "still registered - the server decides. Try one; if it returns a permission "
            "error, grant the permission on the key."
        )
    if not key_known:
        report["api_key_permissions_unknown"] = key.get("error")
        report["note"] = "Could not read key permissions; tools_live reflects configured scopes only."
    return report


def unraid_permissions(args: dict, **kwargs) -> str:
    """Report which unraid_* tools are live and why the others are not."""
    return json.dumps(permission_report())


def _endpoint() -> str:
    return os.environ.get("UNRAID_API_URL", "").strip()


def _ssl_context() -> ssl.SSLContext:
    # Unraid's cert is issued for its myunraid.net hostname; when the endpoint
    # uses a raw LAN IP, verification necessarily fails. Verification is
    # therefore off unless UNRAID_API_VERIFY_TLS=1 is set.
    if os.environ.get("UNRAID_API_VERIFY_TLS") == "1":
        return ssl.create_default_context()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _gql(query: str, variables: dict | None = None, timeout: int | None = None) -> dict:
    """POST a GraphQL query. Returns the decoded response dict.
    Raises urllib/json errors - callers wrap in try/except."""
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        _endpoint(),
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": os.environ.get("UNRAID_API_KEY", ""),
        },
        method="POST",
    )
    with urllib.request.urlopen(
        req, timeout=timeout or _TIMEOUT_SECONDS, context=_ssl_context()
    ) as resp:
        return json.loads(resp.read().decode())


def _run(query: str, variables: dict | None = None, timeout: int | None = None) -> str:
    """Execute a query and return a JSON string per the handler contract."""
    if not _endpoint():
        return json.dumps({"error": "UNRAID_API_URL is not set"})
    if not os.environ.get("UNRAID_API_KEY"):
        return json.dumps({"error": "UNRAID_API_KEY is not set"})
    try:
        result = _gql(query, variables, timeout=timeout)
    except urllib.error.HTTPError as e:
        # A GraphQL validation failure arrives as HTTP 400 with the reason in
        # the body ("Field X must not have a selection since type ... has no
        # subfields"). Discarding it leaves the caller guessing at the schema,
        # so surface it.
        detail = ""
        try:
            body = json.loads(e.read().decode() or "{}")
            errs = body.get("errors") or []
            detail = "; ".join(
                str(x.get("message")) for x in errs if isinstance(x, dict) and x.get("message")
            )
        except Exception:  # noqa: BLE001
            detail = ""
        if detail:
            return json.dumps(
                {"error": f"HTTP {e.code} from Unraid API: {detail}", "http_status": e.code}
            )
        return json.dumps({"error": f"HTTP {e.code} from Unraid API: {e.reason}"})
    except Exception as e:  # noqa: BLE001 - handlers must never raise
        return json.dumps({"error": f"{type(e).__name__}: {e}"})
    if "errors" in result:
        return json.dumps({"error": "GraphQL error", "details": result["errors"]})
    return json.dumps(result.get("data", {}))


def unraid_overview(args: dict, **kwargs) -> str:
    raw = _run(
        """
        { array { state capacity { kilobytes { used total } } }
          docker { containers { names state } }
          info { os { uptime distro release } }
          metrics { cpu { percentTotal } memory { percentTotal } }
          notifications { overview { unread { alert warning info total } } } }
        """
    )
    try:
        data = json.loads(raw)
        if "error" in data:
            return raw
        containers = data.get("docker", {}).get("containers", []) or []
        running = [c for c in containers if c.get("state") == "RUNNING"]
        cap = data.get("array", {}).get("capacity", {}).get("kilobytes", {}) or {}
        used_kb, total_kb = int(cap.get("used", 0)), int(cap.get("total", 0))
        return json.dumps(
            {
                "array_state": data.get("array", {}).get("state"),
                "capacity": {
                    "used_tb": round(used_kb / 1e9, 2),
                    "total_tb": round(total_kb / 1e9, 2),
                    "used_percent": round(100 * used_kb / total_kb, 1) if total_kb else None,
                },
                "os": data.get("info", {}).get("os", {}),
                "utilisation": {
                    "cpu_percent": round((data.get("metrics", {}).get("cpu", {}) or {}).get("percentTotal") or 0, 1),
                    "memory_percent": round((data.get("metrics", {}).get("memory", {}) or {}).get("percentTotal") or 0, 1),
                },
                "containers": {
                    "total": len(containers),
                    "running": len(running),
                    "stopped": len(containers) - len(running),
                    "stopped_names": [
                        n.lstrip("/")
                        for c in containers
                        if c.get("state") != "RUNNING"
                        for n in (c.get("names") or [])
                    ],
                },
                "unread_notifications": data.get("notifications", {}).get("overview", {}).get("unread", {}),
            }
        )
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"{type(e).__name__}: {e}", "raw": raw[:2000]})


def unraid_disks(args: dict, **kwargs) -> str:
    return _run(
        """
        { array {
            disks { name status temp fsUsed fsSize }
            parities { name status temp }
            caches { name status temp fsUsed fsSize } } }
        """
    )


def unraid_containers(args: dict, **kwargs) -> str:
    raw = _run("{ docker { containers { names state image } } }")
    try:
        data = json.loads(raw)
        if "error" in data:
            return raw
        containers = data.get("docker", {}).get("containers", []) or []
        state = (args.get("state") or "").upper()
        needle = (args.get("name_contains") or "").lower()
        out = []
        for c in containers:
            names = [n.lstrip("/") for n in (c.get("names") or [])]
            if state and c.get("state") != state:
                continue
            if needle and not any(needle in n.lower() for n in names):
                continue
            out.append({"name": names[0] if names else "?", "state": c.get("state"), "image": c.get("image")})
        return json.dumps({"count": len(out), "containers": out})
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"{type(e).__name__}: {e}", "raw": raw[:2000]})


def unraid_notifications(args: dict, **kwargs) -> str:
    ntype = (args.get("type") or "UNREAD").upper()
    if ntype not in ("UNREAD", "ARCHIVE"):
        ntype = "UNREAD"
    limit = min(int(args.get("limit") or 10), 50)
    # id is returned so notifications can be archived by the management tool -
    # every mutation keys off it, and it is not otherwise discoverable.
    return _run(
        """
        query($filter: NotificationFilter!) {
          notifications { list(filter: $filter) {
            id subject description importance timestamp } } }
        """,
        {"filter": {"type": ntype, "offset": 0, "limit": limit}},
    )


def unraid_parity(args: dict, **kwargs) -> str:
    # vars.mdResyncSize is deliberately not queried: the upstream API returns
    # it as a 32-bit Int and large arrays overflow it (server-side bug).
    raw = _run(
        """
        { parityHistory { date duration speed status errors }
          vars { mdResyncPos sbSynced sbSyncErrs } }
        """
    )
    try:
        data = json.loads(raw)
        if "error" in data:
            return raw
        v = data.get("vars", {}) or {}
        resync_pos = int(v.get("mdResyncPos") or 0)
        return json.dumps(
            {
                "check_running_now": resync_pos > 0,
                "resync_position": resync_pos,
                "last_sync_epoch": v.get("sbSynced"),
                "last_sync_errors": v.get("sbSyncErrs"),
                "history": data.get("parityHistory", []),
            }
        )
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"{type(e).__name__}: {e}", "raw": raw[:2000]})


def unraid_shares(args: dict, **kwargs) -> str:
    return _run("{ shares { name used free comment } }")


def unraid_metrics(args: dict, **kwargs) -> str:
    return _run("{ metrics { cpu { percentTotal } memory { percentTotal } } }")


def unraid_vms(args: dict, **kwargs) -> str:
    return _run("{ vms { domains { name state } } }")


# ---------------------------------------------------------------------------
# Logs (read-only - no write scope required)
# ---------------------------------------------------------------------------
# Log volume is the practical risk here, not permissions: a syslog tail can
# swamp a model's context. Every path caps lines and truncates the payload,
# and the caps are deliberately modest - ask for more explicitly if needed.

_LOG_LINES_DEFAULT = 100
_LOG_LINES_MAX = 500
_LOG_CHARS_MAX = 20000


def _container_id_by_name(name: str):
    """Resolve a container name to its PrefixedID, or None.

    Deliberately does not consult the protected list: reading a container's
    logs is harmless, including the agent's own, and refusing that would make
    self-diagnosis impossible.
    """
    raw = _run("{ docker { containers { id names } } }")
    data = json.loads(raw)
    if "error" in data:
        raise RuntimeError(data["error"])
    key = str(name).strip().lstrip("/").lower()
    for c in (data.get("docker", {}) or {}).get("containers", []) or []:
        for n in c.get("names") or []:
            if n.lstrip("/").lower() == key:
                return c.get("id")
    return None


def unraid_logs(args: dict, **kwargs) -> str:
    """List server log files, or tail one when a path is given."""
    path = (args.get("path") or "").strip()
    if not path:
        raw = _run("{ logFiles { name path size modifiedAt } }")
        try:
            data = json.loads(raw)
            if "error" in data:
                return raw
            files = data.get("logFiles", []) or []
            # Empty logs are noise when choosing what to read.
            nonempty = [f for f in files if (f.get("size") or 0) > 0]
            return json.dumps(
                {
                    "count": len(nonempty),
                    "hint": "call again with a path to read one",
                    "files": sorted(nonempty, key=lambda f: f.get("modifiedAt") or "", reverse=True),
                }
            )
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": f"{type(e).__name__}: {e}", "raw": raw[:2000]})

    lines = min(max(int(args.get("lines") or _LOG_LINES_DEFAULT), 1), _LOG_LINES_MAX)
    raw = _run(
        "query($path: String!, $lines: Int) { logFile(path: $path, lines: $lines) { path content totalLines startLine } }",
        {"path": path, "lines": lines},
    )
    try:
        data = json.loads(raw)
        if "error" in data:
            return raw
        lf = data.get("logFile", {}) or {}
        content = lf.get("content") or ""
        truncated = len(content) > _LOG_CHARS_MAX
        return json.dumps(
            {
                "path": lf.get("path"),
                "total_lines": lf.get("totalLines"),
                "start_line": lf.get("startLine"),
                "truncated": truncated,
                "content": content[-_LOG_CHARS_MAX:] if truncated else content,
            }
        )
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"{type(e).__name__}: {e}", "raw": raw[:2000]})


def unraid_container_logs(args: dict, **kwargs) -> str:
    """Tail a docker container's logs by container name."""
    name = (args.get("name") or "").strip()
    if not name:
        return json.dumps({"error": "name is required"})
    tail = min(max(int(args.get("tail") or _LOG_LINES_DEFAULT), 1), _LOG_LINES_MAX)
    try:
        cid = _container_id_by_name(name)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"could not list containers: {e}"})
    if not cid:
        return json.dumps({"error": f"no such container: {name}"})
    since = (args.get("since") or "").strip()

    def fetch(use_tail: bool):
        variables = {"id": cid}
        parts_sig, parts_call = ["$id: PrefixedID!"], ["id: $id"]
        if use_tail:
            variables["tail"] = tail
            parts_sig.append("$tail: Int")
            parts_call.append("tail: $tail")
        if since:
            variables["since"] = since
            parts_sig.append("$since: DateTime")
            parts_call.append("since: $since")
        query = (
            f"query({', '.join(parts_sig)}) {{ docker {{ logs({', '.join(parts_call)}) "
            "{ lines { timestamp message } } } }"
        )
        raw_resp = _run(query, variables)
        parsed = json.loads(raw_resp)
        if "error" in parsed:
            return None, raw_resp
        got = ((parsed.get("docker", {}) or {}).get("logs", {}) or {}).get("lines", []) or []
        return got, raw_resp

    try:
        lines, raw = fetch(use_tail=True)
        if lines is None:
            return raw
        # Upstream quirk: the tail argument returns zero lines for some
        # containers that demonstrably have logs (and occasionally returns
        # fewer lines for a larger tail). Retry without it rather than
        # reporting an empty log, which reads as "container is quiet" and is
        # actively misleading when troubleshooting.
        used_fallback = False
        if not lines:
            retry, raw_retry = fetch(use_tail=False)
            if retry:
                lines, raw, used_fallback = retry[-tail:], raw_retry, True
        text = "\n".join(
            f"{ln.get('timestamp') or ''} {ln.get('message') or ''}".strip() for ln in lines
        )
        truncated = len(text) > _LOG_CHARS_MAX
        out = {
            "container": name,
            "line_count": len(lines),
            "truncated": truncated,
            "logs": text[-_LOG_CHARS_MAX:] if truncated else text,
        }
        if used_fallback:
            out["note"] = "tail returned nothing; retried without it (upstream API quirk)"
        if not lines:
            out["warning"] = (
                "The Unraid API returned no log lines for this container. This is a known "
                "upstream limitation for some containers and does NOT mean the container is "
                "silent - check `docker logs` on the host before drawing conclusions."
            )
        return json.dumps(out)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


_MUTATION_RE = re.compile(r"^\s*mutation\b", re.IGNORECASE)


def unraid_graphql(args: dict, **kwargs) -> str:
    # This tool stays read-only unconditionally, even with write scopes
    # enabled. Allowing raw mutations here would bypass every scope check and
    # every protected-container guard below, so the scope system would be
    # decorative. Write actions get their own narrow, guarded tools.
    query = args.get("query") or ""
    if _MUTATION_RE.search(query) or re.search(r"\bmutation\s*[{(]", query, re.IGNORECASE):
        return json.dumps(
            {
                "error": "Mutations are not permitted through unraid_graphql - it is read-only by design.",
                "hint": "Use the dedicated write tools (they enforce scopes and protected containers).",
            }
        )
    if not query.strip():
        return json.dumps({"error": "query is required"})
    return _run(query)


# ---------------------------------------------------------------------------
# Protected containers
# ---------------------------------------------------------------------------
# The agent typically runs *on* the Unraid host it is managing. Updating its
# own container kills the agent mid-call, and the update is reported as a
# failure even when it succeeded. Worse, a sidecar sharing the agent's network
# namespace (network_mode: service:<agent>) is silently orphaned by the
# recreate and has to be recreated itself - it keeps reporting healthy while
# having no working network.
#
# Self-detection covers the agent's own container. Sidecars cannot be detected
# from inside (they are separate containers), so name them explicitly in
# UNRAID_PROTECTED_CONTAINERS.

_self_names_cache = None


def _self_container_names() -> set:
    """Names of the container this code is running in, best-effort.

    Docker sets the hostname to the short container id unless overridden, so
    we match that against the container list. Returns an empty set if we
    cannot tell - callers must not treat that as "nothing is protected".
    """
    global _self_names_cache
    if _self_names_cache is not None:
        return _self_names_cache
    found = set()
    try:
        host = socket.gethostname().strip().lower()
        if host:
            raw = _run("{ docker { containers { id names } } }")
            data = json.loads(raw)
            for c in (data.get("docker", {}) or {}).get("containers", []) or []:
                cid = str(c.get("id") or "").lower()
                # PrefixedID may carry a prefix; compare on the bare id tail.
                bare = cid.split(":")[-1]
                if host and (bare.startswith(host) or host.startswith(bare[:12])):
                    found |= {n.lstrip("/").lower() for n in (c.get("names") or [])}
    except Exception:  # noqa: BLE001 - detection is best-effort, never fatal
        found = set()
    _self_names_cache = found
    return found


def _protected_names() -> set:
    configured = {
        n.strip().lstrip("/").lower()
        for n in str(_settings_mod.load().get("protected_containers") or "").split(",")
        if n.strip()
    }
    return configured | _self_container_names()


def _resolve_containers(names: list) -> tuple:
    """Map container names to ids. Returns (resolved, unknown, protected).

    resolved is a list of {"name","id"}; unknown and protected are name lists.
    """
    raw = _run("{ docker { containers { id names } } }")
    data = json.loads(raw)
    if "error" in data:
        raise RuntimeError(data["error"])
    by_name = {}
    for c in (data.get("docker", {}) or {}).get("containers", []) or []:
        for n in c.get("names") or []:
            by_name[n.lstrip("/").lower()] = c.get("id")
    protected_set = _protected_names()
    resolved, unknown, protected = [], [], []
    for raw_name in names:
        key = str(raw_name).strip().lstrip("/").lower()
        if key in protected_set:
            protected.append(raw_name)
        elif key in by_name:
            resolved.append({"name": raw_name, "id": by_name[key]})
        else:
            unknown.append(raw_name)
    return resolved, unknown, protected


# ---------------------------------------------------------------------------
# Write tools - docker
# ---------------------------------------------------------------------------


def unraid_check_updates(args: dict, **kwargs) -> str:
    """Refresh registry digests, then report which containers have updates.

    isUpdateAvailable reads null until refreshDockerDigests has run, so the
    refresh is not optional - querying alone tells you nothing.
    """
    err = _require("unraid_check_updates")
    if err:
        return err
    refreshed = _run(
        "mutation { refreshDockerDigests }", timeout=_REFRESH_TIMEOUT_SECONDS
    )
    refresh_data = json.loads(refreshed)
    raw = _run("{ docker { containers { names state isUpdateAvailable labels } } }")
    try:
        data = json.loads(raw)
        if "error" in data:
            return raw
        protected_set = _protected_names()
        pending, unknown_state, compose_managed = [], [], []
        for c in (data.get("docker", {}) or {}).get("containers", []) or []:
            name = (c.get("names") or ["?"])[0].lstrip("/")
            avail = c.get("isUpdateAvailable")
            if avail is True:
                pending.append(
                    {"name": name, "protected": name.lower() in protected_set}
                )
            elif avail is None:
                # Unraid derives update status from a container's dockerMan
                # template. Compose-managed containers have none, so they report
                # null forever - that is "not checkable here", not "unknown but
                # retryable", and conflating the two sends you hunting a
                # non-existent fault.
                labels = c.get("labels") or {}
                if isinstance(labels, str):
                    try:
                        labels = json.loads(labels)
                    except Exception:  # noqa: BLE001
                        labels = {}
                if labels.get("com.docker.compose.project"):
                    compose_managed.append(name)
                else:
                    unknown_state.append(name)
        refresh_ok = "error" not in refresh_data
        out = {
            "refresh_ok": refresh_ok,
            "updates_available": [p for p in pending if not p["protected"]],
            "updates_available_but_protected": [
                p["name"] for p in pending if p["protected"]
            ],
            "undetermined": unknown_state,
            "not_checkable_compose_managed": compose_managed,
            "protected_containers": sorted(protected_set),
        }
        if compose_managed:
            out["note_compose"] = (
                "Unraid can only detect updates for containers it manages via a docker "
                "template. Compose-managed containers are not checkable through this API "
                "and must be updated with `docker compose pull` in their project. Their "
                "status here is unknown, not up to date."
            )
        if not refresh_ok:
            out["refresh_error"] = refresh_data.get("error")
            out["warning"] = (
                "Digest refresh did not complete, so 'undetermined' entries are unknown "
                "rather than up to date. Re-run; the server may still have been polling."
            )
        return json.dumps(out)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"{type(e).__name__}: {e}", "raw": raw[:2000]})


def unraid_update_containers(args: dict, **kwargs) -> str:
    """Update named containers to their latest images.

    Names must be listed explicitly. updateAllContainers is deliberately not
    exposed: it takes no arguments and so cannot honour the protected list,
    which would let the agent update itself.
    """
    err = _require("unraid_update_containers")
    if err:
        return err
    names = args.get("names") or []
    if isinstance(names, str):
        names = [names]
    if not names:
        return json.dumps({"error": "names is required (a list of container names)"})
    try:
        resolved, unknown, protected = _resolve_containers(names)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"could not list containers: {e}"})
    if not resolved:
        # Say which of the two reasons applied - "nothing to update" alone
        # reads as "already current", which is the opposite of what happened.
        if protected and not unknown:
            reason = f"every requested container is protected: {', '.join(protected)}"
        elif unknown and not protected:
            reason = f"no such container: {', '.join(unknown)}"
        else:
            reason = "requested containers were either protected or unknown"
        return json.dumps(
            {
                "error": reason,
                "unknown": unknown,
                "refused_protected": protected,
                "protected_containers": sorted(_protected_names()),
            }
        )
    ids = [r["id"] for r in resolved]
    # Pulling and recreating containers routinely exceeds the default
    # timeout. Aborting client-side does not stop the server, so a short
    # timeout reports failure for work that actually succeeds.
    result = _run(
        "mutation($ids: [PrefixedID!]!) { docker { updateContainers(ids: $ids) { id names state } } }",
        {"ids": ids},
        timeout=_MUTATION_TIMEOUT_SECONDS,
    )
    return json.dumps(
        {
            "requested": [r["name"] for r in resolved],
            "refused_protected": protected,
            "unknown": unknown,
            "result": json.loads(result),
        }
    )


def unraid_container_power(args: dict, **kwargs) -> str:
    """Start, stop, or restart a single container."""
    err = _require("unraid_container_power")
    if err:
        return err
    action = (args.get("action") or "").strip().lower()
    if action not in ("start", "stop", "restart"):
        return json.dumps({"error": "action must be one of: start, stop, restart"})
    name = args.get("name")
    if not name:
        return json.dumps({"error": "name is required"})
    try:
        resolved, unknown, protected = _resolve_containers([name])
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"could not list containers: {e}"})
    if protected:
        return json.dumps(
            {"error": f"'{name}' is protected", "protected_containers": sorted(_protected_names())}
        )
    if not resolved:
        return json.dumps({"error": f"no such container: {name}", "unknown": unknown})
    result = _run(
        "mutation($id: PrefixedID!) { docker { %s(id: $id) { id names state } } }" % action,
        {"id": resolved[0]["id"]},
        timeout=_MUTATION_TIMEOUT_SECONDS,
    )
    return json.dumps({"action": action, "name": name, "result": json.loads(result)})


# ---------------------------------------------------------------------------
# Write tools - plugins
# ---------------------------------------------------------------------------


def unraid_install_plugin(args: dict, **kwargs) -> str:
    """Install or update an Unraid plugin from its .plg URL.

    Unraid updates a plugin by reinstalling from the same URL, so this covers
    both cases. The URL is the operator's responsibility - installing a plugin
    is arbitrary code execution on the host.

    THIS MUTATION'S "completed successfully" IS NOT PROOF THE UPDATE ACTUALLY
    APPLIED - confirmed live, 2026-08-19: graphql-api.log logged a plugin
    install (folderview.plus, not dynamix.unraid.net) as started and
    completed successfully 3 seconds apart, but the installed version had NOT
    actually changed afterward - the operator had to separately log into the
    Unraid webGUI for the update to actually take effect. This is a DIFFERENT
    and more general failure mode than the dynamix.unraid.net case below (no
    process crash, no log evidence of anything going wrong at all) - the root
    cause is not yet understood, and it is not currently known whether it
    affects every plugin or only some. Treat this mutation's own response as
    "an install was requested," never as confirmation the plugin actually
    updated. ALWAYS re-check afterward (e.g. via the ucg plugin's
    ucg_plugin_updates with force=true) and treat a plugin that still shows
    update_available after this call as needing a manual webGUI update, not
    as a tool failure to retry.

    Cannot update dynamix.unraid.net (Unraid Connect) itself AT ALL via this
    mutation, for a distinct, well-understood reason - confirmed live,
    2026-08-18: that plugin's install script runs inside the same process
    that serves this GraphQL API, so installing it kills the API server
    mid-install. graphql-api.log showed the process exiting on signal 130
    partway through, then a clean rollback to the prior version on restart -
    no corruption, but the update never actually applies. The only way to
    update that specific plugin is the Unraid webGUI's own "Check for
    Updates" button, which runs the install via PHP outside the Node API
    process and so survives the restart it triggers.
    """
    err = _require("unraid_install_plugin")
    if err:
        return err
    url = (args.get("url") or "").strip()
    if not url:
        return json.dumps({"error": "url is required (the plugin .plg URL)"})
    if not url.lower().startswith("https://"):
        return json.dumps({"error": "url must be https://"})
    payload = {"url": url}
    if args.get("forced"):
        payload["forced"] = True
    return _run(
        "mutation($input: InstallPluginInput!) { unraidPlugins { installPlugin(input: $input) { "
        "id url name status createdAt updatedAt finishedAt output } } }",
        {"input": payload},
    )


# ---------------------------------------------------------------------------
# Write tools - notifications
# ---------------------------------------------------------------------------


def unraid_api_capabilities(args: dict, **kwargs) -> str:
    """List every API field, its required permission, and whether it is in scope.

    Field names are not guessable (the rclone mutation is createRCloneRemote,
    not createRemote), so discovery has to come from the schema rather than
    from the model's priors.
    """
    state = api_map.load(_run)
    if state.get("error"):
        return json.dumps({"error": f"schema introspection failed: {state['error']}"})
    needle = (args.get("contains") or "").lower()
    only_avail = bool(args.get("only_available"))
    namespaces = state.get("namespaces") or {}
    ns_by_type = {v: k for k, v in namespaces.items()}
    rows = []
    for key, (resource, action) in sorted((state.get("map") or {}).items()):
        type_name, field = key.split(".", 1)
        if type_name == "Query":
            call = field
        elif type_name == "Mutation":
            call = f"mutation {field}"
        else:
            call = f"mutation {ns_by_type.get(type_name, type_name)} {{ {field} }}"
        available = has_scope(resource, action)
        if only_avail and not available:
            continue
        if needle and needle not in call.lower() and needle not in resource.lower():
            continue
        rows.append(
            {"call": call, "permission": f"{resource}:{action}", "in_scope": available}
        )
    return json.dumps(
        {
            "total": len(rows),
            "in_scope": sum(1 for r in rows if r["in_scope"]),
            "configured_scopes": sorted(f"{r}:{a}" for r, a in parse_scopes()),
            "fields": rows,
        }
    )


def _protected_ids() -> dict:
    """{lowercased name: id} for protected containers, best effort."""
    out = {}
    try:
        data = json.loads(_run("{ docker { containers { id names } } }"))
        protected = _protected_names()
        for c in (data.get("docker", {}) or {}).get("containers", []) or []:
            for n in c.get("names") or []:
                key = n.lstrip("/").lower()
                if key in protected:
                    out[key] = c.get("id")
    except Exception:  # noqa: BLE001
        pass
    return out


def unraid_api(args: dict, **kwargs) -> str:
    """Run any Unraid API query or mutation, gated on UNRAID_SCOPES.

    Every field in the document is resolved to its RESOURCE:ACTION and checked
    before anything executes. Fields whose permission cannot be determined are
    refused rather than allowed, so an unrecognised or newly added field fails
    closed.
    """
    document = (args.get("document") or "").strip()
    if not document:
        return json.dumps({"error": "document is required"})
    variables = args.get("variables") or {}
    if isinstance(variables, str):
        try:
            variables = json.loads(variables)
        except Exception:  # noqa: BLE001
            return json.dumps({"error": "variables must be a JSON object"})

    is_mutation = bool(re.search(r"^\s*mutation\b|\bmutation\s*[{(]", document, re.I))
    selections = api_map.parse_fields(document)
    if not selections:
        return json.dumps({"error": "could not identify any field in the document"})

    checked, denied = [], []
    for root, nested in selections:
        perm = api_map.permission_for(_run, root, nested, is_mutation=is_mutation)
        label = f"{root}.{nested}" if nested else root
        if perm == "public":
            checked.append({"field": label, "permission": "public"})
            continue
        if perm is None:
            denied.append(
                f"{label}: could not determine required permission, refusing (fail closed)"
            )
            continue
        resource, action = perm
        if has_scope(resource, action):
            checked.append({"field": label, "permission": f"{resource}:{action}"})
        else:
            denied.append(f"{label}: needs {resource}:{action}, not in UNRAID_SCOPES")
    if denied:
        return json.dumps(
            {
                "error": "refused - required permissions are not in scope",
                "denied": denied,
                "allowed": checked,
                "hint": "Use unraid_api_capabilities to see what is available.",
            }
        )

    # Protected containers are a plugin-side concept the server knows nothing
    # about, so the guard has to be applied here. Conservative by design: if a
    # protected container's id or name appears anywhere in a docker mutation,
    # refuse the whole document rather than trying to prove which field it
    # belongs to.
    if is_mutation and any(c["permission"].startswith("DOCKER:") for c in checked):
        haystack = (document + json.dumps(variables)).lower()
        for name, cid in _protected_ids().items():
            if (cid and cid.lower() in haystack) or re.search(
                rf'"/?{re.escape(name)}"', haystack
            ):
                return json.dumps(
                    {
                        "error": f"refused - document references protected container '{name}'",
                        "protected_containers": sorted(_protected_names()),
                    }
                )

    result = _run(document, variables, timeout=_REFRESH_TIMEOUT_SECONDS if is_mutation else None)
    return json.dumps({"permissions_checked": checked, "result": json.loads(result)})


def unraid_notification_manage(args: dict, **kwargs) -> str:
    """Archive or mark-unread Unraid notifications.

    Deletion is deliberately not exposed: archiving clears the unread count
    without destroying the record, which is what "deal with it" nearly always
    means, and it is reversible.
    """
    err = _require("unraid_notification_manage")
    if err:
        return err
    action = (args.get("action") or "").strip().lower()
    if action == "archive":
        ids = args.get("ids") or ([args["id"]] if args.get("id") else [])
        if not ids:
            return json.dumps({"error": "ids (or id) is required for archive"})
        return _run(
            "mutation($ids: [PrefixedID!]!) { archiveNotifications(ids: $ids) { unread { total } } }",
            {"ids": ids},
        )
    if action == "unread":
        nid = args.get("id")
        if not nid:
            return json.dumps({"error": "id is required for unread"})
        return _run(
            "mutation($id: PrefixedID!) { unreadNotification(id: $id) { id type } }",
            {"id": nid},
        )
    if action == "archive_all":
        importance = (args.get("importance") or "").strip().upper()
        if importance and importance not in ("ALERT", "WARNING", "INFO"):
            return json.dumps({"error": "importance must be ALERT, WARNING, or INFO"})
        if importance:
            return _run(
                "mutation($i: NotificationImportance) { archiveAll(importance: $i) { unread { total } } }",
                {"i": importance},
            )
        return _run("mutation { archiveAll { unread { total } } }")
    return json.dumps({"error": "action must be one of: archive, unread, archive_all"})
