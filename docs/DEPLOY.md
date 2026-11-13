# Deploying agent-hub

All shapes start from one root `.env`. Required everywhere: `ADMIN_TOKEN`.
`.env.example` ships it blank and compose refuses to start until it is
set, so no deployment ever runs on a publicly known default token.
The manager is always published on
`127.0.0.1:8000` (same-host adapters and debugging); the `caddy` profile
adds the public TLS endpoint.

## Before any shape: create `.env` once

Run this once, from the repo root, before any section below:

```bash
cp .env.example .env && sed -i.bak "s|^ADMIN_TOKEN=.*|ADMIN_TOKEN=$(openssl rand -hex 32)|" .env && rm .env.bak
```

It copies the template and writes a fresh random `ADMIN_TOKEN` (the
`-i.bak` form works on both GNU and BSD sed). Run it only once: re-running
`cp` overwrites the `.env` you have since edited (`DOMAIN`, Discord vars).
Anyone holding the token has full admin over the hub, so keep `.env`
private. Read it back with:

```bash
grep '^ADMIN_TOKEN=' .env
```

To rotate the token later without touching your other settings, regenerate
it in place (no `cp`):

```bash
sed -i.bak "s|^ADMIN_TOKEN=.*|ADMIN_TOKEN=$(openssl rand -hex 32)|" .env && rm .env.bak
```

Changing `ADMIN_TOKEN` invalidates the bootstrap token used by the Discord
bot and any adapter or agent configured with it as its `AGENT_TOKEN`.
Compose reads `.env` only when containers are created, so a plain
`docker compose restart` keeps the old token. After editing `ADMIN_TOKEN`,
run `docker compose up -d` (with the `--profile` flags you use, e.g.
`docker compose --profile caddy --profile discord up -d`) so the manager
and the Discord bot are recreated with the new value. Then update the
`AGENT_TOKEN` of any adapter or agent that used the old admin token and
restart it.

Sections 1-4 below edit or append lines in your `.env`.

## 1. Localhost (development)

```bash
docker compose up -d
```

Your `.env` already carries `ADMIN_TOKEN` from the step above; compose
refuses to start without it.

Adapter on the same machine: `MANAGER_URL=ws://127.0.0.1:8000/ws/agent`.
No TLS, nothing leaves the machine.

## 2. VPS with a domain (recommended)

Prereqs: a DNS A/AAAA record pointing at the VPS; ports 80 and 443 open.

```env
# in your .env (set the existing DOMAIN line)
DOMAIN=hub.example.com
```

```bash
docker compose --profile caddy up -d
```

Caddy obtains and renews a Let's Encrypt certificate automatically.
Adapters connect to `wss://hub.example.com/ws/agent`.

## 3. VPS, bare IP (no domain)

```env
# in your .env (set the existing DOMAIN line)
DOMAIN=<your-vps-ip>
```

```bash
docker compose --profile caddy up -d
```

Let's Encrypt does not issue certs for bare IPs on the standard endpoint,
so Caddy falls back to its internal CA (self-signed). Adapter machines
must trust that CA. Export the root certificate on the VPS, then copy it
to each adapter machine:

```bash
# on the VPS (the caddy-data volume lives there)
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./root.crt
scp root.crt <adapter-user>@<adapter-host>:

# on the adapter machine (Debian/Ubuntu; adapt for other distros)
sudo cp root.crt /usr/local/share/ca-certificates/agent-hub-ca.crt
sudo update-ca-certificates
# websockets uses the system store, so the adapter now connects cleanly
```

The root certificate path may differ across Caddy versions; if the `cp`
fails, locate it with
`docker compose exec caddy find /data -name root.crt`. The certificate
only exists after Caddy has started with the bare-IP `DOMAIN` at least
once.

Alternatively run a plain-HTTP hub by publishing `8000:8000` (edit the
manager `ports:` in `compose.yml`) — quick-test only, never with a real
token over the public internet.

## 4. Discord frontend

Prereqs: a Discord application + bot (Discord Developer Portal):

1. Bot tab: copy the token → `DISCORD_BOT_TOKEN`; enable the privileged
   **MESSAGE CONTENT INTENT** (startup fails without it).
2. OAuth2 URL generator: scopes `bot`, `applications.commands`; permissions
   View Channels, Send Messages, Create Public Threads, Send Messages in
   Threads, Embed Links (session/run/transcript cards are embeds), Read
   Message History (reply-quoting), Read Message Content; open the URL and
   add the bot to your server.
3. Developer Mode (User Settings > Advanced) → right-click > Copy ID:
   your user id → `DISCORD_OWNER_ID` (owner-gates every command), your
   server id → `DISCORD_GUILD_ID` (instant command sync), optionally a
   channel id → `DISCORD_SYNC_CHANNEL_ID`.

```env
# in your .env (same file as the hub; fill in the existing lines)
DISCORD_BOT_TOKEN=...
DISCORD_OWNER_ID=...
DISCORD_GUILD_ID=...
DISCORD_SYNC_CHANNEL_ID=
```

```bash
docker compose --profile discord up -d
```

`ADMIN_TOKEN` is shared: `compose.yml` passes it to the bot as `HUB_TOKEN`
(the only hub-token variable the bot reads), so you set nothing extra; it
is the one you generated in "create `.env` once".
Verify with `/agent status` in Discord.

## Operations

```bash
docker compose logs -f manager                 # tail the hub
docker compose --profile caddy --profile discord up -d --build   # upgrade after git pull
```

`up -d --build` only rebuilds services that are active, and profile-gated
services (`caddy`, `discord`) are skipped unless their profile is named.
Pass `--profile` only for the profiles you actually use, or set
`COMPOSE_PROFILES=caddy,discord` in `.env` once and drop the flags.

If this deployment was created by an earlier root-run version, run the
one-time chown from "Upgrading from an earlier root-run deployment" (under
Backup and restore, below) before the upgrade command; otherwise the manager
exits with `attempt to write a readonly database`.

### Backup and restore

The manager runs SQLite in WAL mode, so copy the volume only while the
manager is stopped (a live tar can capture an inconsistent database).

The commands below use the volume `agent-hub_manager-data`; that name is
right because `compose.yml` sets `name: agent-hub`. If you set
`COMPOSE_PROJECT_NAME` or pass `-p`, the volume is `<project>_manager-data`
(find it with `docker volume ls | grep manager-data`).

```bash
# backup
docker compose stop manager
docker run --rm -v agent-hub_manager-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/manager-db-$(date +%F).tgz /data
docker compose start manager
```

```bash
# restore (replaces the current database; <file> is the .tgz from above)
docker compose stop manager
docker run --rm -v agent-hub_manager-data:/data -v "$PWD":/backup alpine \
  sh -c 'rm -rf /data/* && tar xzf /backup/<file>.tgz -C /'
# if this backup came from an older root-run deployment, run the one-time
# chown (see "Upgrading from an earlier root-run deployment" below) here
docker compose start manager
```

The manager and Discord images run as uid 10001 (not root). A fresh named
volume needs nothing: Docker copies the image directory's ownership into it
when the volume is empty. The backup and restore commands run as root in an
alpine container and preserve the archived owners, so a backup taken from the
new image restores readable by uid 10001, while a backup from an older
root-run deployment needs the same one-time chown as below.

**Upgrading from an earlier root-run deployment (or restoring a backup made by
one).** An existing `manager-data` volume keeps its root-owned `/data` and
`manager.db`, so the uid-10001 manager fails at startup with
`attempt to write a readonly database`. Run this once, before
`docker compose up -d --build` (volume name per the note above). When
restoring such a backup, run it after the restore's extraction step and
before its final `docker compose start manager`:

```bash
docker run --rm -v agent-hub_manager-data:/data alpine chown -R 10001:10001 /data
```

If `/data` is a host bind mount instead, run `sudo chown -R 10001:10001 <dir>`
once (or run the service with `user: "0:0"`).

The SQLite database is the only stateful component (the frontend owns no
DB; the adapter's journal lives on its own machine).

## Troubleshooting

- **`docker compose` fails with "set ADMIN_TOKEN in .env"**: `ADMIN_TOKEN`
  is blank or `.env` is missing. Generate one into `.env` with the `sed`
  command in "create `.env` once" (`openssl rand -hex 32`). Compose
  evaluates this at parse time, so every compose command (even `logs` or
  `config`) fails until it is set.
- **Adapter fails with a TLS error (`CERTIFICATE_VERIFY_FAILED`) against a
  bare-IP hub**: Caddy serves a certificate from its internal CA, which the
  adapter machine does not trust. Install the root CA there, see "3. VPS,
  bare IP" above. A DNS-named hub with Let's Encrypt needs nothing.
- **`--profile caddy` fails with "port is already allocated" or "address
  already in use" on 80/443**: another service (nginx, Apache, a previous
  stack) holds the port. Stop it; Caddy needs 80 (ACME challenge and
  redirect) and 443 reachable. For a localhost-only hub use the default
  stack without `--profile caddy`.
- **Discord bot restarts in a loop** (`docker compose logs discord`):
  `LoginFailure: Improper token has been passed` means `DISCORD_BOT_TOKEN`
  is wrong (re-copy it from the Developer Portal Bot tab, then
  `docker compose --profile discord up -d`). `PrivilegedIntentsRequired`
  means the MESSAGE CONTENT INTENT is not enabled for the bot, see "4.
  Discord frontend". `DISCORD_BOT_TOKEN is required` means it is blank in
  `.env`.
- **Adapter or agent lost the hub** (hub restart, network drop): nothing to
  do. `agent/local-adapter` retries on its own with exponential backoff
  that doubles from 1 s up to its `reconnect_max` setting (default 30 s,
  `config/adapter.config.toml`), and resets after a successful connect;
  `agent/template` does the same capped at 30 s. Each failed attempt is
  logged, so the adapter log (`journalctl --user -u omp-adapter -f` under
  systemd) shows why. An authorization error (revoked, expired, or rotated
  token) will keep retrying until `AGENT_TOKEN` is fixed.
- **Anything else on the hub side**: `docker compose logs -f manager`.
