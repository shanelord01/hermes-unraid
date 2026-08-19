"""Tool schemas for the Unraid plugin. Descriptions are what the LLM reads -
they say when to use each tool and what it returns."""

OVERVIEW = {
    "name": "unraid_overview",
    "description": (
        "Get the overall status of the Unraid server: array state, used/total "
        "capacity, OS uptime, docker container counts (running/stopped), and "
        "unread notification counts. Use this first for any 'how is the "
        "server?' question; drill down with the other unraid_* tools."
    ),
    "parameters": {"type": "object", "properties": {}},
}

DISKS = {
    "name": "unraid_disks",
    "description": (
        "List every array disk, parity disk, and cache device with status "
        "(e.g. DISK_OK), temperature in Celsius (null if spun down), and "
        "filesystem used/size in kilobytes. Use for disk health, temperature, "
        "or per-disk capacity questions."
    ),
    "parameters": {"type": "object", "properties": {}},
}

CONTAINERS = {
    "name": "unraid_containers",
    "description": (
        "List docker containers on the Unraid host with their state "
        "(RUNNING/EXITED). Optionally filter by state or a name substring. "
        "Use to check whether specific services are up."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "state": {
                "type": "string",
                "enum": ["RUNNING", "EXITED"],
                "description": "Only return containers in this state",
            },
            "name_contains": {
                "type": "string",
                "description": "Case-insensitive substring to match container names",
            },
        },
    },
}

NOTIFICATIONS = {
    "name": "unraid_notifications",
    "description": (
        "List Unraid system notifications (subject, description, importance "
        "ALERT/WARNING/INFO, timestamp). Defaults to unread. Use to see what "
        "the server itself is warning about."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": ["UNREAD", "ARCHIVE"],
                "description": "Which notifications to list (default UNREAD)",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum notifications to return (default 10, max 50)",
            },
        },
    },
}

PARITY = {
    "name": "unraid_parity",
    "description": (
        "Parity check health: history of past checks (date, duration, speed, "
        "status, error count) and whether a check is running right now with "
        "its progress. Use for 'when was the last parity check' or 'is a "
        "parity check running' questions - the key Unraid data-integrity signal."
    ),
    "parameters": {"type": "object", "properties": {}},
}

SHARES = {
    "name": "unraid_shares",
    "description": (
        "List Unraid user shares with used/free space in kilobytes and their "
        "comment/description. Use for 'how full is share X' questions."
    ),
    "parameters": {"type": "object", "properties": {}},
}

METRICS = {
    "name": "unraid_metrics",
    "description": (
        "Live host utilisation: total CPU percent and memory percent right "
        "now. Use for 'how loaded is the server' questions."
    ),
    "parameters": {"type": "object", "properties": {}},
}

VMS = {
    "name": "unraid_vms",
    "description": (
        "List virtual machines on the Unraid host with their state "
        "(RUNNING/SHUTOFF/PAUSED)."
    ),
    "parameters": {"type": "object", "properties": {}},
}

API_CAPABILITIES = {
    "name": "unraid_api_capabilities",
    "description": (
        "Discover the full Unraid API: every query and mutation field, the "
        "RESOURCE:ACTION permission it needs, and whether it is currently in "
        "scope. Use this BEFORE unraid_api - field names are not guessable "
        "(the rclone mutation is createRCloneRemote, not createRemote) and the "
        "list comes from the live schema. Filter with 'contains' to avoid "
        "pulling all ~120 fields."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "contains": {
                "type": "string",
                "description": "Substring filter on the call or resource, e.g. 'parity', 'vm', 'DOCKER'",
            },
            "only_available": {
                "type": "boolean",
                "description": "Only return fields that are in scope right now",
            },
        },
    },
}

API = {
    "name": "unraid_api",
    "description": (
        "Run any Unraid API query or mutation. Every field in the document is "
        "resolved to its required RESOURCE:ACTION and checked against "
        "UNRAID_SCOPES before anything executes; fields whose permission "
        "cannot be determined are refused. This is the full-API escape hatch "
        "for anything the dedicated unraid_* tools do not cover: array and "
        "parity operations, VM control, plugin management, flash backup, UPS, "
        "rclone, API keys. Call unraid_api_capabilities first to find the "
        "correct field name and check it is in scope. Mutations touching a "
        "protected container are refused."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "document": {
                "type": "string",
                "description": (
                    "A GraphQL document, e.g. 'mutation { parityCheck { start } }' "
                    "or '{ upsDevices { name status } }'"
                ),
            },
            "variables": {
                "type": "object",
                "description": "Optional GraphQL variables as a JSON object",
            },
        },
        "required": ["document"],
    },
}

PERMISSIONS = {
    "name": "unraid_permissions",
    "description": (
        "Report which unraid_* tools are currently available and why the "
        "others are not, separating 'not configured in UNRAID_SCOPES' from "
        "'the API key lacks the permission'. Use this first when an unraid "
        "tool is missing or returns a permission error, before assuming the "
        "server is broken."
    ),
    "parameters": {"type": "object", "properties": {}},
}

LOGS = {
    "name": "unraid_logs",
    "description": (
        "Read Unraid server log files. Call with no arguments to list the "
        "available logs (name, path, size, last modified, empty ones omitted), "
        "then call again with a path to read the tail of one. Use for "
        "troubleshooting host-level problems - syslog, docker.log, and plugin "
        "logs all appear here. Output is capped and truncated from the start, "
        "so the most recent lines are always the ones returned."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Full log path from the listing, e.g. /var/log/syslog. Omit to list.",
            },
            "lines": {
                "type": "integer",
                "description": "Lines to read (default 100, max 500)",
            },
        },
    },
}

CONTAINER_LOGS = {
    "name": "unraid_container_logs",
    "description": (
        "Tail a docker container's logs by container name. Use when a "
        "container is unhealthy, restarting, or misbehaving - this is the "
        "first thing to check after unraid_containers shows something in a bad "
        "state. Output is capped and the most recent lines are returned."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Container name, e.g. 'radarr'"},
            "tail": {
                "type": "integer",
                "description": "Lines from the end (default 100, max 500)",
            },
            "since": {
                "type": "string",
                "description": "ISO-8601 timestamp; only return entries after it",
            },
        },
        "required": ["name"],
    },
}


# ---------------------------------------------------------------------------
# Write tools. Registered only when their RESOURCE:UPDATE_ANY permission is
# present in UNRAID_SCOPES, so the model never sees a tool it cannot use.
# ---------------------------------------------------------------------------

CHECK_UPDATES = {
    "name": "unraid_check_updates",
    "description": (
        "Check which docker containers on the Unraid host have a newer image "
        "available. Refreshes registry digests first, which is required - "
        "update status reads as unknown until that runs. Returns containers "
        "with updates pending, which of those are protected from actuation, "
        "and any whose status could not be determined. Use before "
        "unraid_update_containers."
    ),
    "parameters": {"type": "object", "properties": {}},
}

UPDATE_CONTAINERS = {
    "name": "unraid_update_containers",
    "description": (
        "Update one or more named docker containers to their latest image. "
        "Names must be given explicitly; there is no update-everything option, "
        "because that could not exclude protected containers. Protected "
        "containers (including the one this agent runs in) are refused and "
        "reported back rather than silently skipped. Run unraid_check_updates "
        "first to see what actually needs updating."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Container names to update, e.g. ['radarr','sonarr']",
            },
        },
        "required": ["names"],
    },
}

CONTAINER_POWER = {
    "name": "unraid_container_power",
    "description": (
        "Start, stop, or restart a single docker container on the Unraid host. "
        "Protected containers are refused. Use for recovering a stopped "
        "service, not as a routine action."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Container name"},
            "action": {
                "type": "string",
                "enum": ["start", "stop", "restart"],
                "description": "What to do with the container",
            },
        },
        "required": ["name", "action"],
    },
}

INSTALL_PLUGIN = {
    "name": "unraid_install_plugin",
    "description": (
        "Install or update an Unraid plugin from its .plg URL. Unraid updates "
        "a plugin by reinstalling from the same URL, so this covers both. "
        "This API alone cannot tell you which plugins have updates available "
        "(installedUnraidPlugins returns names only, no version/update flag) "
        "- if the ucg plugin is available, use its ucg_plugin_updates tool "
        "for that instead of guessing. "
        "Installing a plugin executes code on the host as root - only use URLs "
        "the operator has specified or that are already installed. "
        "THIS TOOL REPORTING SUCCESS IS NOT PROOF THE UPDATE ACTUALLY "
        "APPLIED - confirmed live 2026-08-19 with a plugin other than "
        "dynamix.unraid.net: the API logged the install as completed "
        "successfully, but the version had not actually changed until the "
        "operator separately used the Unraid webGUI. Root cause not "
        "understood; not known whether this affects every plugin or only "
        "some. ALWAYS re-check afterward (ucg_plugin_updates with force=true, "
        "if available) rather than trusting this call's own response - if "
        "the plugin still shows as needing an update, report that as an "
        "action item for the operator (Unraid webGUI's own Update button), "
        "do not just retry this tool. "
        "CANNOT update dynamix.unraid.net (Unraid Connect) itself AT ALL via "
        "this tool, for a separate, well-understood reason: that plugin runs "
        "the GraphQL API service this tool goes through, so installing it "
        "kills the API process mid-install. It rolls back safely, but the "
        "update never actually applies - confirmed live, not theoretical. "
        "Report this rather than retrying; the operator needs the Unraid "
        "webGUI's own Update button for this one plugin, which installs via "
        "PHP outside the API process and survives the restart."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The plugin .plg URL (https only)"},
            "forced": {
                "type": "boolean",
                "description": "Reinstall even if already up to date (default false)",
            },
        },
        "required": ["url"],
    },
}

NOTIFICATION_MANAGE = {
    "name": "unraid_notification_manage",
    "description": (
        "Archive Unraid notifications or mark one unread. Archiving clears the "
        "unread count without deleting the record. Get ids from "
        "unraid_notifications first. Note: archiving an alert only dismisses "
        "the message - it does not fix the underlying condition, and a "
        "recurring scan such as Fix Common Problems will raise it again on its "
        "next run. Do not archive warnings to make a problem look resolved."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["archive", "unread", "archive_all"],
                "description": "archive (by ids), unread (single id), or archive_all",
            },
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Notification ids to archive",
            },
            "id": {"type": "string", "description": "Single notification id"},
            "importance": {
                "type": "string",
                "enum": ["ALERT", "WARNING", "INFO"],
                "description": "For archive_all, limit to this importance level",
            },
        },
        "required": ["action"],
    },
}

GRAPHQL = {
    "name": "unraid_graphql",
    "description": (
        "Run a raw read-only GraphQL query against the Unraid API for data the "
        "other unraid_* tools don't cover (shares, network, VMs, etc.). "
        "Mutations are refused client-side and the API key is read-only "
        "server-side. Tip: introspect with "
        "{ __type(name: \"Query\") { fields { name } } } to discover fields."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A GraphQL query document (queries only, no mutations)",
            },
        },
        "required": ["query"],
    },
}
