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
| `unraid_api_capabilities` | none | Discover all ~120 API fields, their permissions, and what is in scope |
| `unraid_api` | per field | Run any query or mutation, permission-checked field by field |
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

## Full API access

The dedicated tools above cover the common cases. Everything else in the API - array and parity operations, VM control, plugin and API key management, flash backup, UPS, rclone, docker folders - is reachable through `unraid_api`, without registering 120 separate tools.

It is not an unchecked escape hatch. The Unraid schema documents its own authorisation in field descriptions:

```
Action: **UPDATE_ANY**   Resource: **DOCKER**
```

120 of 151 fields carry that, so the permission map is **introspected from the live schema** rather than hand-maintained, and cannot drift as Unraid adds fields. The remaining 31 (notification mutations, UPS and public queries) come from a small verified fallback table.

Every field in a submitted document is resolved and checked before anything executes:

```
mutation { array { setState(input:{desiredState:STOP}) { state } } }
-> refused: array.setState needs ARRAY:UPDATE_ANY, not in UNRAID_SCOPES

mutation { totallyMadeUpField { x } }
-> refused: could not determine required permission (fail closed)
```

Unrecognised fields are refused rather than allowed, so a newly added or misspelled field fails closed. The protected-container guard applies here too: a docker mutation referencing a protected container's name or id is refused outright.

Use `unraid_api_capabilities` first. Field names are not guessable - the rclone mutation is `createRCloneRemote`, not `createRemote`.

## Alerts: Unraid pushes to the agent

The plugin subscribes to `notificationsWarningsAndAlerts` over `graphql-transport-ws`. New warnings and alerts - disk errors, parity problems, Fix Common Problems findings - reach the agent when they happen rather than whenever something next polls.

Agent messages are delivered as Unraid notifications via `createNotification`, so they appear in the webGUI notification centre and flow through whatever notification agents Unraid has configured. That also makes `deliver=unraid` usable as a cron delivery target.

### Rate limiting is on by default

Every forwarded alert can wake the agent and cost model tokens, so the limits are defaults rather than opt-in:

```env
UNRAID_ALERTS_ENABLED=true          # master switch
UNRAID_ALERT_MIN_IMPORTANCE=WARNING # INFO | WARNING | ALERT
UNRAID_ALERT_COOLDOWN_SECONDS=300   # per subject
UNRAID_ALERT_MAX_PER_HOUR=20        # hard ceiling
UNRAID_OUTBOUND_ENABLED=true        # deliver agent messages to Unraid
```

The cooldown keys on **subject**, not notification id, because a flapping condition raises a fresh id every time and an id-based cooldown would never catch it.

### Deduplication and backlog handling

`notificationsWarningsAndAlerts` returns a **list** and re-sends the whole current set whenever anything changes. Without dedupe by id, one new alert would re-announce every outstanding one. The adapter dedupes, and treats the first payload after connecting as the existing backlog - seeding the dedupe set rather than emitting it. Otherwise every gateway restart would replay all open alerts.

Inbound needs `NOTIFICATIONS:READ_ANY`, which the `VIEWER` role already grants. Outbound additionally needs `NOTIFICATIONS:CREATE_ANY`; without it, sending returns a message naming the missing permission rather than a bare `FORBIDDEN`.

## Protected containers

When the agent runs on the host it manages, updating its own container kills the agent mid-call and reports a failure even when the update succeeded. Worse, a sidecar sharing the agent's network namespace (`network_mode: service:<agent>`) is silently orphaned by the recreate: it keeps reporting healthy while having no working network.

The agent's own container is detected automatically. Sidecars cannot be detected from inside, so name them:

```env
UNRAID_PROTECTED_CONTAINERS=hermes,hermes-ts
```

Write tools refuse protected containers and say so, rather than skipping them silently. `updateAllContainers` is deliberately not exposed at all: it takes no arguments and therefore cannot honour this list.

## Setup

Four steps: create a scoped API key, point the plugin at your server, install it, and check what registered.

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

Only `UNRAID_API_URL` and `UNRAID_API_KEY` are required. Everything else can be set here or from the dashboard tab once the plugin is running - see [Configuration](#configuration) for how the two interact.

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

## Configuration

Settings resolve in this order:

1. **The settings file**, `$HERMES_HOME/unraid_settings.json` - written by the dashboard tab
2. **Environment variables**
3. **Built-in defaults**

A field is only taken from the file when it is present and non-empty, so clearing it in the dashboard hands control back to the environment rather than storing a blank. Once the dashboard has saved a field, editing the corresponding environment variable has no effect until the field is cleared.

| Setting | Environment variable | Default |
|---|---|---|
| API scopes | `UNRAID_SCOPES` | `*:READ_ANY` |
| Protected containers | `UNRAID_PROTECTED_CONTAINERS` | none |
| Forward alerts | `UNRAID_ALERTS_ENABLED` | `true` |
| Minimum importance | `UNRAID_ALERT_MIN_IMPORTANCE` | `WARNING` |
| Alert cooldown (seconds) | `UNRAID_ALERT_COOLDOWN_SECONDS` | `300` |
| Alerts per hour | `UNRAID_ALERT_MAX_PER_HOUR` | `20` |
| Outbound notifications | `UNRAID_OUTBOUND_ENABLED` | `true` |
| Verify TLS | `UNRAID_API_VERIFY_TLS` | off |

### Dashboard tab

The plugin adds an **Unraid** tab to the Hermes dashboard with toggles for alert forwarding, importance threshold, cooldown, hourly cap, outbound notifications, API scopes and protected containers. Each field is labelled with where its value came from, so it is clear whether the dashboard, an environment variable or a default is in control.

Alert settings are read per event, so changes take effect without a restart. Scope changes alter which tools are registered and need a gateway restart.

## Limitations

Three things the Unraid API cannot do, all upstream rather than plugin behaviour. Each is reported explicitly rather than being reported as a clean result.

### Plugin updates cannot be detected
`unraid_install_plugin` installs or updates a plugin from its `.plg` URL, and Unraid treats an update as a reinstall from the same URL. What the API cannot do is tell you which plugins need one: `installedUnraidPlugins` returns `[String!]!`, a list of names with no version or update flag. Update detection lives in Community Applications' web UI and is not exposed.

So the agent can apply a plugin update you name, but cannot discover that one is available.

### Update detection only covers template-managed containers
`unraid_check_updates` depends on Unraid's own digest comparison, which works off a container's docker template. Containers created by Compose Manager have no template, so `isUpdateAvailable` reports null for them permanently. That is "not checkable here", not "unknown but retryable".

The tool separates the two rather than lumping them together:

- `updates_available` - has a newer image
- `undetermined` - genuinely unknown, e.g. a private registry Unraid cannot query
- `not_checkable_compose_managed` - compose-managed, update with `docker compose pull` in the project

Note also that `refreshDockerDigests` contacts every registry serially and routinely takes over a minute. The plugin allows 240s for it; a shorter client timeout aborts while the server keeps polling, leaving digests half-populated.

### Container logs
`unraid_container_logs` depends on the API's `docker.logs` query, which is unreliable for some containers. Observed on Unraid API v4.37:

Some containers return the requested lines correctly. Others return fewer lines for a larger `tail`, and some return nothing at all despite having tens of thousands of log lines. The plugin retries without `tail` when it gets zero lines, and when it still gets nothing it says so explicitly, because a false "no logs" reads as "the container is quiet" and is actively misleading during troubleshooting.

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
