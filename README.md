# hermes-unraid

A [Hermes Agent](https://hermes-agent.nousresearch.com/) plugin that gives your agent visibility into, and optionally control over, an Unraid server - array health, disk status and temperatures, docker container states, host and container logs, and system notifications - via Unraid's official built-in GraphQL API (Unraid 7+).

Built for the common homelab case where Hermes itself runs in a Docker container *on* the Unraid host: the agent's shell only sees its own container, and you very deliberately do not want to hand it SSH or the Docker socket.

**Read-only by default.** Actuation (container updates, lifecycle, plugin installs, notification management) is opt-in per scope, using Unraid's own permission grammar.

## Permission model

Every tool declares a `RESOURCE:ACTION` permission in the same vocabulary Unraid uses for API keys, so what you put in `UNRAID_SCOPES` matches what you granted the key.

```env
UNRAID_SCOPES="DOCKER:READ_ANY,DOCKER:UPDATE_ANY,LOGS:READ_ANY"
UNRAID_SCOPES="DOCKER:*,LOGS:READ_ANY"   # every action on DOCKER
UNRAID_SCOPES="*:READ_ANY"               # everything, read-only (the default)
```

Wildcards work on either side. A bare resource (`DOCKER`) means every action on it. Unset defaults to `*:READ_ANY`.

Two independent locks must be open for a write to succeed:

1. **`UNRAID_SCOPES`** decides which tools get registered at all. Out-of-scope tools are never presented to the model, so it cannot plan around a capability you did not grant.
2. **The API key's own permissions**, enforced by Unraid server-side. This is the lock that actually matters. A `VIEWER` key refuses mutations no matter what the scopes say.

Run `unraid_permissions` (always registered) to see which tools are live and, for the ones that are not, whether it is your scope config or your API key.

## Tools

| Tool | Permission | What it does |
|------|-----------|--------------|
| `unraid_permissions` | none | Which tools are live and why the others are not |
| `unraid_overview` | `ARRAY:READ_ANY` | Array state, capacity, uptime, container counts, unread notifications |
| `unraid_parity` | `ARRAY:READ_ANY` | Parity check history and whether one is running now |
| `unraid_disks` | `DISK:READ_ANY` | Every array/parity/cache disk with status, temperature, usage |
| `unraid_containers` | `DOCKER:READ_ANY` | Docker containers with state, filterable |
| `unraid_notifications` | `NOTIFICATIONS:READ_ANY` | Unraid notifications, including ids needed to archive them |
| `unraid_shares` | `SHARE:READ_ANY` | User shares with used/free space |
| `unraid_metrics` | `INFO:READ_ANY` | Live CPU and memory utilisation |
| `unraid_vms` | `VMS:READ_ANY` | Virtual machines and their states |
| `unraid_logs` | `LOGS:READ_ANY` | List server log files, or tail one (syslog, docker.log, plugin logs) |
| `unraid_container_logs` | `LOGS:READ_ANY` | Tail a container's logs by name |
| `unraid_graphql` | `INFO:READ_ANY` | Raw GraphQL escape hatch; **queries only, mutations always refused** |
| `unraid_check_updates` | `DOCKER:UPDATE_ANY` | Refresh registry digests and report which containers have updates |
| `unraid_update_containers` | `DOCKER:UPDATE_ANY` | Update named containers to their latest images |
| `unraid_container_power` | `DOCKER:UPDATE_ANY` | Start, stop, or restart a container |
| `unraid_notification_manage` | `NOTIFICATIONS:UPDATE_ANY` | Archive notifications or mark one unread |
| `unraid_install_plugin` | `CONFIG:UPDATE_ANY` | Install or update an Unraid plugin from its .plg URL |

Also registers a `/unraid` slash command for a quick status from any connected chat platform.

## Protected containers

When the agent runs on the host it manages, updating its own container kills the agent mid-call and reports a failure even when the update succeeded. Worse, a sidecar sharing the agent's network namespace (`network_mode: service:<agent>`) is silently orphaned by the recreate: it keeps reporting healthy while having no working network.

The agent's own container is detected automatically. Sidecars cannot be detected from inside, so name them:

```env
UNRAID_PROTECTED_CONTAINERS=hermes,hermes-ts
```

Write tools refuse protected containers and say so, rather than skipping them silently. `updateAllContainers` is deliberately not exposed at all: it takes no arguments and therefore cannot honour this list.

## Setup

### 1. Create an API key (on the Unraid host)

Read-only:

```bash
unraid-api apikey --create --name hermes --roles VIEWER
```

For actuation, grant only the permissions you intend to use:

```bash
unraid-api apikey --create --name hermes --roles VIEWER \
  --permissions "DOCKER:UPDATE_ANY,NOTIFICATIONS:UPDATE_ANY"
```

Avoid an ADMIN-role key. Grant the narrow permission instead.

### 2. Set environment variables for Hermes

```env
UNRAID_API_URL=https://<unraid-ip>:<webgui-ssl-port>/graphql   # e.g. https://192.168.1.10:5001/graphql
UNRAID_API_KEY=<the key from step 1>

# Optional
UNRAID_SCOPES=DOCKER:READ_ANY,LOGS:READ_ANY
UNRAID_PROTECTED_CONTAINERS=hermes
```

The endpoint is your webGUI address plus `/graphql` (check Settings, Management Access for the port). TLS note: Unraid's certificate is issued for its `myunraid.net` hostname, so verification against a raw LAN IP fails and the plugin skips TLS verification by default. Set `UNRAID_API_VERIFY_TLS=1` to enforce it if your `UNRAID_API_URL` uses the proper hostname.

### 3. Install the plugin

```bash
hermes plugins install shanelord01/hermes-unraid
```

Or manually: clone into `~/.hermes/plugins/unraid/` (that is `/opt/data/plugins/unraid/` in the Docker image) and restart the gateway.

### 4. Verify

```bash
hermes -z "Which unraid tools do you have available, and what is my server status?"
```

The gateway log reports the scopes in effect, the key's name and roles, how many tools registered, and a warning for anything in scope the key appears unable to do.

## Known upstream limitation: container logs

`unraid_container_logs` depends on the API's `docker.logs` query, which is unreliable for some containers. Observed on Unraid API v4.37:

| Container | API `tail=5` | API no `tail` | Actual `docker logs` |
|---|---|---|---|
| grafana | 5 | 200 | 7,304 |
| hermes | 0 | 7 | 264 |
| traccar | 0 | 0 | 119,450 |

Asking for more lines can return fewer, and some containers return nothing at all. The plugin retries without `tail` when it gets zero lines, and when it still gets nothing it says so explicitly, because a false "no logs" reads as "the container is quiet" and is actively misleading during troubleshooting.

Server log files (`unraid_logs`) do not have this problem.

## Fix Common Problems

FCP alerts surface through Unraid's notification system, so `unraid_notifications` sees them and `unraid_notification_manage` can archive them. There is no FCP-specific API: the findings list and its ignore rules live on the flash drive and are not reachable here. Archiving an FCP notification dismisses the message without fixing anything, and FCP will raise it again on its next scan. The tool descriptions say so, so the model does not treat archiving as a resolution.

## Security posture

- **Read-only by default.** Actuation requires naming a scope *and* an API key that permits it.
- **`unraid_graphql` refuses mutations unconditionally**, even with write scopes enabled. Allowing raw mutations there would bypass every scope check and the protected-container list, making the whole model decorative.
- **Deletion is not exposed.** Notifications can be archived, which is reversible; `removeContainer` and the notification delete mutations are not wired up.
- **No shell, no SSH, no Docker socket** required or requested.
- Client-side scopes are an intent and tool-surface layer, not a security boundary. The API key's permissions are the boundary. Scope narrowly.

## Requirements

- Unraid 7.x with the built-in `unraid-api` (ships with the OS; built and tested against API v4.37)
- Hermes Agent with plugin support
- Python stdlib only - no extra dependencies

## License

MIT
