# Adding an agent

Any process that speaks protocol v1 can join the fleet. The hub needs no
changes — agents are clients of `/ws/agent`.

## Protocol v1 in one table

JSON envelopes `{protocol_version, type, request_id?, session_id?, payload}`.
Unknown types are ignored by both sides, so the protocol evolves additively.
The type constants live in `hub/manager/app/protocol.py` (mirrored by
`agent/template/template_agent/protocol.py`).

| Type | Direction | Purpose |
|---|---|---|
| `agent.register` / `agent.heartbeat` | A→H | identity + liveness; register payload carries `agent_id` (must match the token's pinned id), `name`, `description`, `capabilities` |
| `session.create` / `session.message` / `session.cancel` | H→A | drive runs |
| `session.cancel_ack` | A→H | acknowledge a cancel |
| `session.waiting` / `session.activity` / `session.delta` | A→H | live progress (`session.waiting` with `{"state": "created"}` is also the `session.create` ack) |
| `agent.message` | A→H | the reply text |
| `session.completed` / `session.failed` | A→H | terminal state (`failed` payload `{"reason": ...}`) |
| `sessions.snapshot`, `sync.*`, `delegate.*` | A⇄H | optional: journal sync + delegation (see docs/ARCHITECTURE.md) |

## Request correlation (what the hub waits for)

The hub treats two H→A frames as synchronous calls (`session.create` and,
for some agents, `session.cancel`): it sends the frame with a fresh
`request_id` and matches a reply on **both** `session_id` and
`request_id`. A reply with a missing or different `request_id` is not
matched. A third frame, `session.message`, is not a call but carries a
`request_id` the agent must echo.

- **`session.create`** (opening a session, `POST /sessions`, Discord
  `/session new`): the hub waits up to 30 s. Reply with `session.waiting`
  (`{"state": "created"}`) to accept, or `session.failed`
  (`{"reason": "..."}`) to refuse; the reason is surfaced to the caller.
  Only `session.failed` refuses: any other reply whose `session_id` and
  `request_id` both match the create frame is accepted. Silence, or a
  reply with a missing or mismatched `session_id`/`request_id`, times out
  and fails the call as "agent unreachable" (HTTP 409); the session row is
  marked failed. Always echo both ids from the create frame.
- **`session.cancel`**: reply `session.cancel_ack` with the same
  `session_id` and `request_id`. The hub only sends a `request_id` and
  waits for the ack (one retry) for agents that register the
  `owns_sessions` capability; for other agents the cancel is
  fire-and-forget with no `request_id`. Echoing `request_id` (which is
  `null` in that case) is correct either way. The ack is not the outcome:
  the run still ends with `session.failed {"reason": "cancelled"}` (or
  `session.completed` if it finished first).
- **`session.message`**: not a call. Echo the message's `request_id` on
  every frame of that run (`session.delta`, `session.activity`,
  `session.waiting`, `agent.message`, `session.completed`,
  `session.failed`). The hub stores the `agent.message` `request_id` on the
  transcript row, so replies stay tied to the prompt that produced them.

`agent/template` follows all of this: it always accepts `session.create`
(there is nothing to provision), acks `session.cancel` with the request's
ids, and echoes the message `request_id` on its delta, message, completed
and cancelled-failed frames. It does not emit `session.activity` or a
running `session.waiting`, ignores `workspace`, and does not register
`owns_sessions`, so the hub keeps the session truth and sends it
fire-and-forget cancels. Refusing a create (`session.failed`) is up to your
agent; the template never does.

## Steps

Prerequisites: a running hub, Python 3.12 (`python3`), `curl`, and `jq`
(the verify step pipes `curl` through `jq` to read the session id).

1. **Copy the skeleton.** `cp -r agent/template/ my-agent/` — it mirrors
   `hub/manager/app/protocol.py` and implements register → create ack →
   reply → cancel. For a real runtime, replace `template_agent/agent.py`'s
   `_run` with your process spawning logic (see `agent/local-adapter` for
   subprocess handling, journals, and resume).
2. **Mint a token.** The REST examples below use two shell variables you
   define yourself: `HUB_URL`, the hub's base URL, and `ADMIN_TOKEN`, the
   value from the hub's `.env`. (This `HUB_URL` is only a shell variable for
   your `curl` calls; it is unrelated to the compose-managed `HUB_URL` the
   Discord container uses.)

   ```bash
   export HUB_URL=https://<your-host>      # or http://127.0.0.1:8000 locally
   export ADMIN_TOKEN=<ADMIN_TOKEN from the hub .env>
   ```

   Tokens are pinned to one `agent_id`; the agent must register with
   exactly that id:

   ```bash
   curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"agent_id": "my-agent"}' $HUB_URL/tokens
   ```

   (or Discord `/agent token new my-agent`). The secret is shown once.
   Optional `ttl_days` in the body overrides the default expiry (30 d).
   Rotate with the call below (or Discord `/agent token rotate`); rotation
   revokes the old secret and drops the live connection:

   ```bash
   curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" $HUB_URL/tokens/my-agent/rotate
   ```
3. **Configure and run.**

   ```bash
   cd my-agent
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   MANAGER_URL=wss://<your-host>/ws/agent AGENT_TOKEN=<token> AGENT_ID=my-agent \
     .venv/bin/python -m template_agent
   ```

   (`AGENT_ID` defaults to `template`; set it to the id the token was
   minted for. `AGENT_NAME` and `AGENT_DESCRIPTION` are optional. Use
   `ws://127.0.0.1:8000/ws/agent` against a local hub.)
4. **Verify.**
   `curl -H "Authorization: Bearer $ADMIN_TOKEN" $HUB_URL/agents` → your
   agent lists `online` with its capabilities. Then open a session and talk
   to it:

   ```bash
   SID=$(curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"agent_id": "my-agent", "owner_id": "me"}' $HUB_URL/sessions | jq -r .id)
   curl -s -X POST -H "Authorization: Bearer $ADMIN_TOKEN" \
     -H "Content-Type: application/json" -d '{"text": "hi"}' \
     $HUB_URL/sessions/$SID/messages
   curl -s -H "Authorization: Bearer $ADMIN_TOKEN" $HUB_URL/sessions/$SID/messages
   ```

## Capabilities

`capabilities` in `agent.register` is a free-form list the UI surfaces
(`chat`, `cancel`, `streaming`, `activity` are what local-adapter
declares, plus `owns_sessions`, which flips the hub into projection mode
for journal-owning adapters — omit it unless you implement the sync
protocol). The hub does not otherwise enforce capabilities; declare what
your agent honors.
