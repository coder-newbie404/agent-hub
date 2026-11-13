# template agent

Minimal protocol-v1 agent: registers with the hub, acks `session.create`
(`session.waiting`), answers every `session.message` with one streamed canned
reply, honors cancel. It echoes the hub's `request_id` (with the same
`session_id`) on the create ack, the cancel ack, and every run frame — the
hub matches acks on both ids and tags transcript rows with the message id
(see docs/AGENTS.md, "Request correlation").
Use it as the copy-me starting point for real agents — `agent/local-adapter`
is the full-featured reference (journal, sync, resume, delegation).

## Run

Requires Python >= 3.12 (repo standard) and websockets >= 14 (the
`additional_headers` auth-header argument of `websockets.connect` needs >= 14).

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    export MANAGER_URL=ws://127.0.0.1:8000/ws/agent   # your hub
    export AGENT_TOKEN=<per-agent token>
    .venv/bin/python -m template_agent

Optional env: `AGENT_ID` (default `template`), `AGENT_NAME`,
`AGENT_DESCRIPTION`, `REPLY`. Mint a token with
`POST /tokens {"agent_id": "..."}` using `ADMIN_TOKEN`, or Discord
`/agent token new` — see docs/AGENTS.md.

## Tests

    .venv/bin/pip install -r requirements-dev.txt
    .venv/bin/python -m pytest tests/ -q
