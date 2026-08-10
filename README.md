# hermes-unraid

A [Hermes Agent](https://hermes-agent.nousresearch.com/) plugin that gives your agent **read-only** visibility into an Unraid server - array health, disk status and temperatures, docker container states, and system notifications - via Unraid's official built-in GraphQL API (Unraid 7+).

Built for the common homelab case where Hermes itself runs in a Docker container *on* the Unraid host: the agent's shell only sees its own container, and you very deliberately do not want to hand it SSH or the Docker socket. This plugin closes the visibility gap while keeping actuation impossible - the API key it uses cannot change anything, enforced server-side by Unraid.

## Tools

| Tool | What it does |
|------|--------------|
| `unraid_overview` | Array state, capacity, uptime, container counts (with names of stopped ones), unread notification counts |
| `unraid_disks` | Every array/parity/cache disk with status, temperature, and usage |
| `unraid_containers` | Docker containers with state, filterable by state or name |
| `unraid_notifications` | Unraid's own notifications (subject, importance, timestamp) |
| `unraid_graphql` | Raw read-only GraphQL escape hatch for anything else (shares, network, VMs); mutations refused |

Also registers a `/unraid` slash command for a quick status from any connected chat platform.

## Setup

### 1. Create a scoped, read-only API key (on the Unraid host)

```bash
unraid-api apikey --create --name hermes --roles VIEWER \
  --permissions "ARRAY:READ_ANY,DOCKER:READ_ANY,DISK:READ_ANY,INFO:READ_ANY,NOTIFICATIONS:READ_ANY"
```

Do **not** use an ADMIN-role key. The whole point is that this credential is constitutionally incapable of modifying your server.

### 2. Set environment variables for Hermes

```env
UNRAID_API_URL=https://<unraid-ip>:<webgui-ssl-port>/graphql   # e.g. https://192.168.1.10:5001/graphql
UNRAID_API_KEY=<the key from step 1>
```

The endpoint is your webGUI address + `/graphql` (check Settings → Management Access for the port). TLS note: Unraid's certificate is issued for its `myunraid.net` hostname, so verification against a raw LAN IP fails; the plugin skips TLS verification by default. Set `UNRAID_API_VERIFY_TLS=1` to enforce it if your `UNRAID_API_URL` uses the proper hostname.

### 3. Install the plugin

```bash
hermes plugins install shanelord01/hermes-unraid
```

Or manually: clone into `~/.hermes/plugins/unraid/` (that's `/opt/data/plugins/unraid/` in the Docker image) and restart the gateway.

### 4. Verify

```bash
hermes -z "Give me the current status of my unraid server"
```

## Security posture

- **Read-only by three layers**: the API key's permissions (server-side, the layer that actually matters), a client-side mutation refusal in `unraid_graphql`, and tool descriptions that tell the model not to attempt changes.
- **No shell, no SSH, no Docker socket** required or requested.
- If the agent needs to *act* on findings (restart a container, etc.), keep that out of band: have it report and recommend, and run the action yourself.

## Requirements

- Unraid 7.x with the built-in `unraid-api` (ships with the OS; this plugin was built against API v4.36)
- Hermes Agent with plugin support
- Python stdlib only - no extra dependencies

## License

MIT
