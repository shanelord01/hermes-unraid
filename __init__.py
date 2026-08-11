"""Hermes Agent plugin: Unraid server management via the official GraphQL API.

One plugin, two halves. Tools let the agent ask; the platform adapter lets
Unraid tell, by subscribing to warnings and alerts and delivering messages back
as Unraid notifications. Home Assistant looks like a single plugin only because
its tools live in Hermes core; a user plugin cannot do that, but it can
register both here - the loader only gates `kind` for bundled plugins.

Every tool declares a RESOURCE:ACTION permission in Unraid's own grammar.
UNRAID_SCOPES decides which get registered and defaults to *:READ_ANY, so the
plugin is read-only until actuation is granted explicitly. Tools that are out
of scope are never registered, so the model is not shown capabilities the
operator has not granted.
"""

import logging

from . import schemas, tools

_log = logging.getLogger(__name__)

# tool name -> schema. The required permission lives in tools.TOOL_PERMISSIONS
# so there is one source of truth shared by registration and the runtime guard.
_TOOLS = (
    ("unraid_overview", schemas.OVERVIEW, tools.unraid_overview),
    ("unraid_disks", schemas.DISKS, tools.unraid_disks),
    ("unraid_containers", schemas.CONTAINERS, tools.unraid_containers),
    ("unraid_notifications", schemas.NOTIFICATIONS, tools.unraid_notifications),
    ("unraid_parity", schemas.PARITY, tools.unraid_parity),
    ("unraid_shares", schemas.SHARES, tools.unraid_shares),
    ("unraid_metrics", schemas.METRICS, tools.unraid_metrics),
    ("unraid_vms", schemas.VMS, tools.unraid_vms),
    ("unraid_logs", schemas.LOGS, tools.unraid_logs),
    ("unraid_container_logs", schemas.CONTAINER_LOGS, tools.unraid_container_logs),
    ("unraid_graphql", schemas.GRAPHQL, tools.unraid_graphql),
    ("unraid_check_updates", schemas.CHECK_UPDATES, tools.unraid_check_updates),
    ("unraid_update_containers", schemas.UPDATE_CONTAINERS, tools.unraid_update_containers),
    ("unraid_container_power", schemas.CONTAINER_POWER, tools.unraid_container_power),
    ("unraid_install_plugin", schemas.INSTALL_PLUGIN, tools.unraid_install_plugin),
    ("unraid_notification_manage", schemas.NOTIFICATION_MANAGE, tools.unraid_notification_manage),
)


def _startup_check(registered: list) -> None:
    """Log what is live, and separate config gaps from API-key gaps.

    Two independent things can disable a tool: the scope not being configured,
    or the API key lacking the permission. Without this the second case only
    shows up as a runtime error deep inside a task, where it looks like a bug.
    Best-effort: it makes one API call and must never prevent registration.
    """
    try:
        report = tools.permission_report()
    except Exception as e:  # noqa: BLE001
        _log.warning("[unraid] permission check skipped: %s", e)
        return

    _log.info(
        "[unraid] scopes=%s key=%s roles=%s",
        ",".join(report.get("configured_scopes") or []) or "none",
        report.get("api_key_name") or "unknown",
        ",".join(report.get("api_key_roles") or []) or "unknown",
    )
    _log.info("[unraid] %d tool(s) registered: %s", len(registered), ", ".join(registered))

    blocked = report.get("tools_likely_blocked_by_api_key") or []
    for item in blocked:
        # In scope but the key cannot do it: the actionable misconfiguration.
        _log.warning("[unraid] in scope, API key may lack permission - %s", item)
    if blocked:
        _log.warning(
            "[unraid] fix with: unraid-api apikey --create --name hermes "
            "--permissions \"DOCKER:UPDATE_ANY,...\" (grant only what you need)"
        )
    if report.get("api_key_permissions_unknown"):
        _log.warning(
            "[unraid] could not read API key permissions (%s); "
            "registration reflects configured scopes only",
            report["api_key_permissions_unknown"],
        )


def register(ctx):
    registered = []
    for name, schema, handler in _TOOLS:
        if not tools.tool_allowed(name):
            continue
        ctx.register_tool(name=name, toolset="unraid", schema=schema, handler=handler)
        registered.append(name)

    # Always available. unraid_permissions is the tool you need precisely when
    # nothing else registered. The other two cover the rest of the API without
    # a tool per field: capabilities reads the schema, and unraid_api resolves
    # and enforces each field's permission at call time, so neither can be
    # gated on one RESOURCE:ACTION up front.
    for name, schema, handler in (
        ("unraid_permissions", schemas.PERMISSIONS, tools.unraid_permissions),
        ("unraid_api_capabilities", schemas.API_CAPABILITIES, tools.unraid_api_capabilities),
        ("unraid_api", schemas.API, tools.unraid_api),
    ):
        ctx.register_tool(name=name, toolset="unraid", schema=schema, handler=handler)
        registered.append(name)

    def _handle_unraid(raw_args: str) -> str:
        return tools.unraid_overview({})

    ctx.register_command(
        "unraid",
        handler=_handle_unraid,
        description="Quick Unraid server status (array, containers, notifications)",
    )

    # Platform half. Imported lazily: it depends on gateway.* and aiohttp,
    # which are present at runtime but absent when the module is imported
    # standalone for testing. A failure here must not cost you the tools.
    try:
        from . import platform_adapter
        platform_adapter.register_platform(ctx)
        _log.info("[unraid] platform adapter registered (alerts in, notifications out)")
    except Exception as e:  # noqa: BLE001
        _log.warning("[unraid] platform adapter unavailable, tools still active: %s", e)

    _startup_check(registered)
