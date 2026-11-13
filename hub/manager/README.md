# agent-hub / manager

The hub: FastAPI + SQLite service. Runs under Docker Compose (root
`compose.yml`; quickstart in the repo-root `README.md`, TLS modes in
`docs/DEPLOY.md`): exposes the agent WebSocket at `/ws/agent`, the UI event
channel at `/ws/ui`, and the REST API frontends and tooling consume.

**Frontends are separate components** (see `frontend/` at the repo root); the Discord bot
is no longer part of this process. The hub pushes every session/sync event
(final messages, streaming deltas, activity, completion/failure, agent
disconnects, sync batches) to connected UI clients on `/ws/ui`; everything
else a frontend needs crosses the REST API.

## Configuration

Variables are split by secret-ness:

- **env** (root `.env`, loaded by `compose.yml`): secrets and
  deployment-critical values.
- **config file** (`config/manager.config.toml`, committed): component
  tuning knobs. The root `compose.yml` mounts `hub/manager/config`
  read-only at `/srv/config`, so the default path
  (`config/manager.config.toml`, relative to the container WORKDIR `/srv`)
  resolves to it: edit the file, then `docker compose restart manager`.
  `MANAGER_CONFIG` can override the path; env vars always win over the file.

| Env variable  | Required | Default            | Meaning |
|---------------|----------|--------------------|---------|
| `ADMIN_TOKEN` | yes      | —                  | Bootstrap/admin credential: admin REST + `/ws/ui` (the Discord frontend's credential) + wildcard agent WS access. It is re-seeded from this env value at every boot, so it stays valid; keep it for the owner UI/frontend and give agents per-agent tokens. Rotating this env value rotates the bootstrap token at next boot. |
| `SQLITE_PATH` | no       | `/data/manager.db` | SQLite database path (`/data` is the compose volume). |

| Config file key      | Default | Meaning |
|----------------------|---------|---------|
| `token_ttl_days`     | `30`    | Agent tokens expire after this many days; `/agent token rotate` mints a replacement. |
| `heartbeat_interval` | `30`    | Seconds between offline-sweep ticks. |
| `offline_after`      | `180`   | Seconds without a heartbeat before an agent is marked offline. |

## REST API

Auth: `Authorization: Bearer <ADMIN_TOKEN>`. Hub-domain errors come back as
HTTP statuses with a machine code in `detail.code` (`not_found` → 404,
`run_in_progress` / `agent_offline` → 409, `agent_failed` → 502).

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness (open) |
| GET | `/agents` | agent rows + live `online` flag (connection map) |
| GET | `/sessions[?thread_id=]` | list, or look a session up by Discord thread |
| POST | `/sessions` | create a session through an agent (sync RPC) |
| GET | `/sessions/{id}` | session detail (timestamps included) |
| PATCH | `/sessions/{id}` | rename / archive / attach thread |
| DELETE | `/sessions/{id}` | delete (409 while a run is in flight) |
| GET | `/sessions/{id}/messages[?unposted=true]` | transcript / missed rendering queue |
| POST | `/sessions/{id}/messages` | send a user prompt to the agent |
| POST | `/sessions/{id}/messages/mark-posted` | mark messages rendered (sync bookkeeping) |
| GET | `/sessions/{id}/messages/count` | transcript size |
| GET | `/sessions/{id}/run` | `{inflight, activity}` for the run card |
| POST | `/sessions/{id}/cancel` | cancel the in-flight run |
| GET | `/sessions/missed` | sessions with unrendered messages |
| POST | `/sessions/recover-interrupted` | startup recovery: RUNNING/WAITING → FAILED |
| POST | `/sync/nudge` | push `sync.now` to every online locally-owning agent |
| POST/GET | `/tokens` | mint / list per-agent tokens |
| POST | `/tokens/{agent_id}/rotate` | revoke active tokens, mint a replacement |
| POST | `/tokens/{agent_id}/revoke` | revoke the agent's tokens |

### Agent tokens

Every agent authenticates to `/ws/agent` with a **per-agent bearer token**
(sha-256-hashed at rest, pinned to one `agent_id`, expiring after
`token_ttl_days`). Mint/rotate/revoke from Discord (frontend) or REST:

- `POST /tokens` — mint; the secret is shown **once**
- `POST /tokens/{agent_id}/rotate` — revoke active tokens, mint a replacement
- `POST /tokens/{agent_id}/revoke` — revoke; the agent drops offline immediately
- `GET /tokens` — state of all tokens

## Local development

```bash
cd hub/manager
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
SQLITE_PATH=./dev.db .venv/bin/uvicorn app.main:app --ws-max-size 67108864 --reload
```

The app creates its schema on startup (idempotent); no migration step needed
for dev. Alembic migrations live in `migrations/`.

## Tests

```bash
cd hub/manager
.venv/bin/python -m pytest tests/ -v
```
