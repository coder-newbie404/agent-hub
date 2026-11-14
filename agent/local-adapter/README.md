# local-adapter

Outbound-only WebSocket daemon that runs on your local machine. It connects to
the hub (`wss://<your-host>/ws/agent`), registers as an agent,
and spawns/cancels local `omp` runs on behalf of Discord sessions. It never
listens on a port.

## Configuration

Variables are split by secret-ness:

- **env** (`.env` in this directory; env names matched
  case-insensitively): important + secret values.
- **config file** (`config/adapter.config.toml`, committed): component
  tuning. `ADAPTER_CONFIG` overrides the path.

Precedence, highest first: real process environment (shell exports,
systemd `EnvironmentFile`), then the config file, then the `.env` file,
then the code defaults. So a real environment variable beats the file,
but a key set in the config file beats the same key in `.env`: that is why
the committed file leaves `omp_bin` unset, so `OMP_BIN` in `.env` works.
Keep secrets and deployment-specific values in `.env`, not in the file.

| Env variable       | Required | Meaning |
|--------------------|----------|---------|
| `MANAGER_URL`      | yes      | Manager WebSocket endpoint, e.g. `wss://<your-host>/ws/agent`. |
| `AGENT_TOKEN`      | yes      | **Per-agent** bearer token minted by the hub (`/agent token new <agent_id>` or `POST /tokens`). Pinned to your `AGENT_ID`. |
| `DEFAULT_WORKSPACE`| yes      | Workspace passed to `omp` when a session has none. |
| `ALLOWED_ROOT`     | no       | If set, workspaces must live under this directory. |

For TLS modes and what URL to use per deployment shape, see docs/DEPLOY.md.

See `config/adapter.config.toml` for the component tuning (agent id/name,
`omp` binary path, journal/state paths, heartbeat/reconnect/sync cadence,
stream size limit) with inline comments.

## Install and run

```bash
cd agent/local-adapter
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m agent_adapter
```

There is no packaged console script yet (no `pyproject.toml`), so `python -m
agent_adapter` from this directory is the canonical way to run it. If you add a
`pyproject.toml` with a console entry point, `pipx install -e .` gives you
`~/.local/bin/agent-adapter` instead.

TLS depends on how the hub is deployed: a DNS-named hub with a Let's
Encrypt cert works with the default system trust store; a bare-IP hub uses
Caddy's internal CA, which this machine must trust — see docs/DEPLOY.md.

## Delegate to other agents (for runs)

A running `omp` process (or any script) can reach the manager through the
adapter without handling tokens: the CLI writes a request file into the
delegation drop box and waits for the daemon's answer. The daemon must be
running with `delegation_enabled = true` — that is the code default
(`agent_adapter/config.py`); override it in `config/adapter.config.toml` or
with the `DELEGATION_ENABLED` env var.

```bash
cd agent/local-adapter
.venv/bin/python -m agent_adapter.cli agents
.venv/bin/python -m agent_adapter.cli sessions --agent <agent_id>
.venv/bin/python -m agent_adapter.cli create --agent <agent_id> --description "..."
.venv/bin/python -m agent_adapter.cli grant --session <session_id> --agent <agent_id> --level read   # or write
.venv/bin/python -m agent_adapter.cli ask --session <session_id> --text "..." --hop 0 --timeout 600
```

Every command accepts `--timeout S` (default 600) for how long to wait,
prints the result JSON on stdout, and exits `0` on success, `1` on
timeout, `2` when the broker answered with an error payload.

Drop-box contract: the CLI writes `~/.omp-adapter/delegations/req-<id>.json`
(override the directory with `DELEGATION_DIR`) and polls for
`req-<id>.result.json`, containing `{"ok": true, "data": ...}` or
`{"ok": false, "error": ..., "detail"?}`. The adapter's watcher picks the
request up within its poll interval and removes it after answering.

Manual smoke (live hub): start the daemon with `delegation_enabled = true`
(the default), then run the ops above against it — `agents` lists every
online agent, `sessions` shows the sessions visible to you, `create` opens a
session on another online agent, a two-round `ask` (first answer
`needs_input` with options, then `final`) returns the final reply, and
`grant` makes the transcript readable to the other agent.

## Run under systemd (user unit)

```bash
mkdir -p ~/.config/systemd/user
cp systemd/agent-adapter@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-adapter@<name>
loginctl enable-linger $USER   # keep it running after logout
```

The unit's `WorkingDirectory`/`EnvironmentFile`/`ExecStart` point at
`/opt/agent-hub/agent/local-adapter` — edit all three if your checkout
lives elsewhere. `EnvironmentFile` requires `.env` to exist.

systemd's `PATH` usually lacks `~/.local/bin`, so set `OMP_BIN=/absolute/path/to/omp` in the adapter `.env` before enabling the service (the unit loads it via `EnvironmentFile`, i.e. as real process environment), otherwise the first session fails with `FileNotFoundError`. For manual runs a `.env` `OMP_BIN` works too, because the committed `config/adapter.config.toml` does not set `omp_bin`; if you do set `omp_bin` there, it overrides a `.env` value (but not a real environment variable).

Hardening: the adapter `.env` holds `AGENT_TOKEN`, so `chmod 600 .env`. Also set `ALLOWED_ROOT` to the one directory tree the agent may work in; without it, anyone who obtains a hub token can point omp at arbitrary directories on this machine.

Operate it with:

```bash
journalctl --user -u agent-adapter@<name> -f
systemctl --user restart agent-adapter@<name>
systemctl --user stop agent-adapter@<name>
```

### Migrating from omp-adapter

Earlier versions shipped the unit as `omp-adapter.service` and the package as
`omp_adapter`. The unit is now the template `agent-adapter@.service` (instance
name `<name>`) and the module is run as `python -m agent_adapter`. To migrate:
`systemctl --user disable --now omp-adapter`, remove the old unit file from
`~/.config/systemd/user/`, copy the new one, and enable `agent-adapter@<name>`.

## Tests

```bash
cd agent/local-adapter
.venv/bin/python -m pytest tests/ -v
```
