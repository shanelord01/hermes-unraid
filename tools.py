"""Tool handlers for the Unraid plugin.

Contract per Hermes plugin rules: handlers accept (args: dict, **kwargs),
always return a JSON string, and never raise.
"""

import json
import os
import re
import ssl
import urllib.error
import urllib.request

_TIMEOUT_SECONDS = 15


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


def _gql(query: str, variables: dict | None = None) -> dict:
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
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS, context=_ssl_context()) as resp:
        return json.loads(resp.read().decode())


def _run(query: str, variables: dict | None = None) -> str:
    """Execute a query and return a JSON string per the handler contract."""
    if not _endpoint():
        return json.dumps({"error": "UNRAID_API_URL is not set"})
    if not os.environ.get("UNRAID_API_KEY"):
        return json.dumps({"error": "UNRAID_API_KEY is not set"})
    try:
        result = _gql(query, variables)
    except urllib.error.HTTPError as e:
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
    return _run(
        """
        query($filter: NotificationFilter!) {
          notifications { list(filter: $filter) {
            subject description importance timestamp } } }
        """,
        {"filter": {"type": ntype, "offset": 0, "limit": limit}},
    )


_MUTATION_RE = re.compile(r"^\s*mutation\b", re.IGNORECASE)


def unraid_graphql(args: dict, **kwargs) -> str:
    query = args.get("query") or ""
    if _MUTATION_RE.search(query) or re.search(r"\bmutation\s*[{(]", query, re.IGNORECASE):
        return json.dumps({"error": "Mutations are not permitted - this plugin is read-only by design."})
    if not query.strip():
        return json.dumps({"error": "query is required"})
    return _run(query)
