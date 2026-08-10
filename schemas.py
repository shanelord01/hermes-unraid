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
