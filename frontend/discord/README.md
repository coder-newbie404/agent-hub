# frontend/discord — Discord frontend

The Discord UI for the agent hub: slash commands, thread↔session mapping,
live run cards, transcript rendering, token management. A standalone
container that talks to the hub over REST + the `/ws/ui` event stream —
it owns no database and no hub state.

## Configuration (env, via the root `.env` + `compose.yml`)

| Env variable             | Required | Default              | Meaning |
|--------------------------|----------|----------------------|---------|
| `HUB_URL`                | no       | `http://manager:8000` | Hub base URL (compose sets it). |
| `HUB_TOKEN`              | yes      | —                    | Hub bearer for REST + `/ws/ui`. `compose.yml` derives it from the root `.env` `ADMIN_TOKEN` (`HUB_TOKEN: ${ADMIN_TOKEN}`); set it directly only when running outside compose. |
| `DISCORD_BOT_TOKEN`      | yes      | `""`                 | Discord bot token; the bot exits fast when unset. |
| `DISCORD_OWNER_ID`       | yes      | `0`                  | Your Discord user id; owner-gates all commands. |
| `DISCORD_GUILD_ID`       | no       | `None`               | If set, slash commands sync to that guild instantly instead of globally. |
| `DISCORD_SYNC_CHANNEL_ID`| no       | `None`               | Home channel for auto-created threads when sync discovers threadless sessions. |
| `TOKEN_TTL_DAYS`         | no       | `30`                 | `/agent token new` default expiry. Container env only: `compose.yml` does not forward it; add it under the discord service `environment:` in `compose.yml` if you need to change it. |
| `STREAM_INTERVAL`        | no       | `2.0`                | Minimum seconds between live-preview message edits (Discord rate-limit guard). Container env only: `compose.yml` does not forward it; add it under the discord service `environment:` in `compose.yml` if you need to change it. |

## Behavior highlights

- While an agent runs, the session thread shows one live preview message:
  the current activity (`⚙ bash: …`, `⚙ thinking`) plus the streamed reply
  text, edited at most once per `STREAM_INTERVAL` seconds. When the run
  finishes, the live message is flipped to its terminal state and the
  **full reply** is posted (chunked to Discord's limits). It is persisted
  hub-side and available via `GET /sessions/{id}`.
- **Message sync:** every persisted message carries a `posted_at` timestamp —
  `NULL` means it has never been rendered in Discord. Missed rows are printed by
  the periodic sync loop (paced by the adapter's `SYNC_INTERVAL`, default 60 s),
  on demand via `/sync`, and by the transcript reprint that runs when
  `/session open`, the session picker's "Open in new thread" button, or a
  RelinkButton binds an old session to a fresh thread. A Discord 4xx rejection
  (anything but 429) is logged with a warning and that message is given up on
  rather than retried forever. A session with no thread gets one auto-created in
  `DISCORD_SYNC_CHANNEL_ID`.
- Slash commands: `/session new|list|info|rename|archive|delete|open`,
  `/resume`, `/run status|cancel`, `/agent status`, `/agent token new|rotate|revoke|list`,
  `/sync`, `/help` (all owner-gated; replying to an earlier message in the
  session thread quotes it as context for the agent).

## Run

Container (preferred): from the repo root, with the discord keys set in the root `.env`
(`DISCORD_BOT_TOKEN`, `DISCORD_OWNER_ID`, `DISCORD_GUILD_ID`,
`DISCORD_SYNC_CHANNEL_ID`; see the root `README.md` and `docs/DEPLOY.md`):

```bash
docker compose --profile discord up -d --build
```

Local:

```bash
cd frontend/discord
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
HUB_URL=http://localhost:8000 HUB_TOKEN=... DISCORD_BOT_TOKEN=... \
  DISCORD_OWNER_ID=... .venv/bin/python -m discord_frontend
```

## Tests

```bash
cd frontend/discord
.venv/bin/python -m pytest tests/ -v
```
