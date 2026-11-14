# local-adapter

Outbound-only WebSocket daemon that runs on your local machine. It connects to
the hub (`wss://<your-host>/ws/agent`), registers as an agent, and spawns and
cancels runs of a local coding-agent CLI (its *runtime*) on behalf of Discord
sessions. It never listens on a port. One adapter process drives exactly one
runtime; run one instance per runtime (see "Running several instances").

## Runtimes

| Runtime | Binary | Streaming | Policies enforced | Status |
|---|---|---|---|---|
| `omp` | `omp` | token deltas + tool activity | `default`, `readonly`, `edits`, `full` (omp `--approval-mode`: none, `always-ask`, `write`, `yolo`) | verified live (omp 18.1.19) |
| `claude` | `claude` | token deltas + tool activity | `default`, `readonly` (`plan`), `edits` (`acceptEdits`), `full` (`bypassPermissions`) | **experimental**: verified against fixtures only, never run against a real `claude` binary here |
| `opencode` | `opencode` | tool activity only, no token streaming (the reply arrives whole) | `default`, `full` only | verified live. Set `MODEL`: the configured default model may be missing. Headless permission asks are auto-rejected unless the policy is `full` |

Policies (`POLICY`; a runtime that cannot enforce the chosen one refuses to
start):

- `default`: no flags; the runtime's own configuration decides.
- `readonly`, `edits`, `full`: increasingly permissive, mapped to the
  runtime's own permission flags (table above).

What was observed for `omp` on 18.1.19 (headless `--mode=json`, one probe
prompt per mode: create a file, then run `echo hi`): `readonly` denied both
the file write and the shell command and exited cleanly without hanging;
`edits` allowed the write and denied the shell command; `full` allowed both.
That is the evidence behind the table, not a security guarantee: read-only
tools were not exercised under `readonly`, and one prompt per mode is a thin
sample. Treat any policy as a convenience, not a sandbox.

## Configuration

Variables are split by secret-ness:

- **env** (a real process environment, the systemd `EnvironmentFile`, or a
  `.env` file in this directory; env names matched case-insensitively):
  important + secret values and everything that differs per instance.
- **config file** (`config/adapter.config.toml`, committed): component
  tuning only. `ADAPTER_CONFIG` overrides the path.

Precedence, highest first: real process environment (shell exports, systemd
`EnvironmentFile`), then the config file, then the `.env` file, then the code
defaults. A key set in the config file beats the same key in `.env`, which is
why the committed file sets only pure tuning keys and nothing per-instance.

| Setting (env name) | Required | Meaning |
|---|---|---|
| `MANAGER_URL` | yes | Manager WebSocket endpoint, e.g. `wss://<your-host>/ws/agent`. |
| `AGENT_TOKEN` | yes | **Per-agent** bearer token minted by the hub (`/agent token new <agent_id>` or `POST /tokens`). Pinned to your `AGENT_ID`. |
| `DEFAULT_WORKSPACE` | yes | Workspace passed to the runtime when a session has none. |
| `RUNTIME` | yes | `omp`, `claude` or `opencode`. |
| `RUNTIME_BIN` | no | Binary to run. Default: the runtime name on `PATH`. Set an absolute path under systemd (its `PATH` usually lacks `~/.local/bin`). |
| `POLICY` | no | `default` (default), `readonly`, `edits`, `full`; must be supported by the runtime (table above). |
| `MODEL` | no | Model to use; unset = the runtime's own default. opencode wants `provider/model`. |
| `RUNTIME_ARGS` | no | JSON list of extra argv for the runtime, inserted before the prompt, e.g. `["--some-flag"]`. |
| `AGENT_ID` | no | Identity registered with the hub. Default `<runtime>-local`. Unique per instance. |
| `AGENT_NAME` | no | Display name. Default `<runtime> local`. |
| `ENV_PASSTHROUGH` | no | JSON list of extra environment variable *name patterns* (fnmatch) the agent process may see; see "Environment the agent sees". |
| `ALLOWED_ROOT` | no, recommended | If set, workspaces must live under this directory. |
| `ALLOW_PAYLOAD_ENV` | no | Default `false`. A test hook: when `true`, an `env` object in the hub's session payload is added to the agent process environment. Leave off in real deployments. |
| `JOURNAL_DIR`, `STATE_FILE`, `DELEGATION_DIR` | no | Default under `~/.agent-adapter/<AGENT_ID>/` (`journal/`, `state.json`, `delegations/`). |

For TLS modes and what URL to use per deployment shape, see docs/DEPLOY.md.

`config/adapter.config.toml` carries the pure tuning keys (heartbeat,
reconnect, sync cadence, stream size limit, `delegation_enabled`, and
`omp_titles_db`, which only applies to `RUNTIME=omp`) with inline comments.

## Install and run

```bash
cd agent/local-adapter
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env     # set MANAGER_URL, AGENT_TOKEN, DEFAULT_WORKSPACE, RUNTIME
.venv/bin/python -m agent_adapter
```

There is no packaged console script (no `pyproject.toml`), so `python -m
agent_adapter` from this directory is the canonical way to run it.

TLS depends on how the hub is deployed: a DNS-named hub with a Let's
Encrypt cert works with the default system trust store; a bare-IP hub uses
Caddy's internal CA, which this machine must trust — see docs/DEPLOY.md.

## Running several instances (systemd)

The unit is a template, `systemd/agent-adapter@.service`: one instance per
runtime, each with its own env file `~/.config/agent-adapter/<instance>.env`,
its own `AGENT_ID`, and its own hub token.

```bash
mkdir -p ~/.config/systemd/user ~/.config/agent-adapter
cp systemd/agent-adapter@.service ~/.config/systemd/user/
systemctl --user daemon-reload

# one instance per runtime
cp .env.example ~/.config/agent-adapter/omp.env      # RUNTIME=omp
cp .env.example ~/.config/agent-adapter/claude.env   # RUNTIME=claude, AGENT_ID=claude-local
chmod 600 ~/.config/agent-adapter/*.env
$EDITOR ~/.config/agent-adapter/claude.env

systemctl --user enable --now agent-adapter@omp
systemctl --user enable --now agent-adapter@claude
loginctl enable-linger $USER   # keep them running after logout
```

Each env file needs its own `AGENT_ID` and an `AGENT_TOKEN` minted for that id
(a token is pinned to one agent id). Data lives apart per instance under
`~/.agent-adapter/<AGENT_ID>/`.

The unit's `WorkingDirectory` and `ExecStart` point at
`/opt/agent-hub/agent/local-adapter`: edit both if your checkout lives
elsewhere. `EnvironmentFile` requires the instance env file to exist. Do not
put inline comments after a value in these files: systemd keeps them as part
of the value.

systemd's `PATH` usually lacks `~/.local/bin`, so set `RUNTIME_BIN` to the
absolute path of the CLI in each env file, otherwise the first session fails
with `FileNotFoundError`.

Operate an instance with:

```bash
journalctl --user -u agent-adapter@<instance> -f
systemctl --user restart agent-adapter@<instance>
systemctl --user stop agent-adapter@<instance>
```

## Environment the agent sees

The agent process does not inherit the adapter's environment. It gets an
allow-list:

- a base set: `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LANGUAGE`,
  `TERM`, `TMPDIR`, `TZ`, proxy variables (`HTTP_PROXY`, `HTTPS_PROXY`,
  `NO_PROXY`, and lowercase forms), CA-bundle variables (`SSL_CERT_FILE`,
  `SSL_CERT_DIR`, `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`), and `LC_*` /
  `XDG_*`;
- the runtime's credential variables (the `auth_env` patterns in its driver
  module: `*_API_KEY` for omp and opencode, plus `OPENCODE_*` for opencode;
  Anthropic/Claude/cloud-provider variables for claude);
- any name matching an `ENV_PASSTHROUGH` pattern;
- `ADAPTER_DELEGATION_DIR` (see "Delegate to other agents"), when delegation
  is enabled.

Everything else is dropped, notably `AGENT_TOKEN`, and any variable named like
an adapter setting (`MANAGER_URL`, `POLICY`, `MODEL`, ...). If a runtime needs
another variable (for example `GOOGLE_CLOUD_PROJECT`), add it:
`ENV_PASSTHROUGH=["GOOGLE_CLOUD_PROJECT"]`.

## Security notes

- Hardening: the instance env file holds `AGENT_TOKEN`, so `chmod 600` it.
- Set `ALLOWED_ROOT` to the one directory tree the agent may work in. Without
  it, anyone who obtains a hub token can point the runtime at arbitrary
  directories on this machine.
- `POLICY=full` removes the runtime's permission prompts. Use it only on hosts
  where the agent is sandboxed (container, VM, throwaway user).
- Claude Code runs the project's own hooks (`.claude/settings*.json`) in
  headless mode without prompting: a repository you point it at can execute
  commands. Only point a `claude` instance at workspaces you trust.

## Delegate to other agents (for runs)

A running agent process (or any script) can reach the manager through the
adapter without handling tokens: the CLI writes a request file into the
delegation drop box and waits for the daemon's answer. The daemon must be
running with `delegation_enabled = true` — that is the code default
(`agent_adapter/config.py`); override it in `config/adapter.config.toml` or
with the `DELEGATION_ENABLED` env var. While it is enabled, the adapter
exports `ADAPTER_DELEGATION_DIR` to every agent process automatically, which
is how the CLI finds the drop box.

```bash
cd agent/local-adapter
.venv/bin/python -m agent_adapter.cli agents
.venv/bin/python -m agent_adapter.cli sessions --agent <agent_id>
.venv/bin/python -m agent_adapter.cli create --agent <agent_id> --description "..."
.venv/bin/python -m agent_adapter.cli grant --session <session_id> --agent <agent_id> --level read   # or write
.venv/bin/python -m agent_adapter.cli ask --session <session_id> --text "..." --hop 0 --timeout 600
```

Outside an agent process (a shell of your own), set `ADAPTER_DELEGATION_DIR`
to the instance's drop box (`~/.agent-adapter/<AGENT_ID>/delegations`) or pass
`--dir <dir>` after the command.

Every command accepts `--timeout S` (default 600) for how long to wait,
prints the result JSON on stdout, and exits `0` on success, `1` on
timeout, `2` when the broker answered with an error payload.

Drop-box contract: the CLI writes `req-<id>.json` into the drop-box directory
and polls for `req-<id>.result.json`, containing `{"ok": true, "data": ...}` or
`{"ok": false, "error": ..., "detail"?}`. The adapter's watcher picks the
request up within its poll interval and removes it after answering.

Manual smoke (live hub): start the daemon with `delegation_enabled = true`
(the default), then run the ops above against it — `agents` lists every
online agent, `sessions` shows the sessions visible to you, `create` opens a
session on another online agent, a two-round `ask` (first answer
`needs_input` with options, then `final`) returns the final reply, and
`grant` makes the transcript readable to the other agent.

## Migrating from omp-adapter

Earlier versions shipped one `omp`-only service: the unit `omp-adapter.service`,
the package `omp_adapter`, the variable `OMP_BIN`, and data in
`~/.omp-adapter`. The adapter now supports several runtimes and one instance
per runtime. To keep your `omp` setup:

1. Stop the old service: `systemctl --user disable --now omp-adapter`, then
   remove its unit file from `~/.config/systemd/user/`.
2. Create the instance env file `~/.config/agent-adapter/omp.env` (from
   `.env.example`) from your old adapter `.env`: rename `OMP_BIN` to
   `RUNTIME_BIN`, add `RUNTIME=omp`, and keep `AGENT_ID=omp-local` (the old
   default) so the hub identity and your token stay valid.
3. Move the data directory:
   `mkdir -p ~/.agent-adapter && mv ~/.omp-adapter ~/.agent-adapter/omp-local`
   (the last component is your `AGENT_ID`). The adapter refuses to start until
   this is done, so an old journal is never orphaned silently.
4. The agent process no longer inherits the adapter's whole environment. If
   it relied on a variable that is not a `*_API_KEY`, add it to
   `ENV_PASSTHROUGH`.
5. Install the template unit and enable the instance:
   `cp systemd/agent-adapter@.service ~/.config/systemd/user/ && systemctl
   --user daemon-reload && systemctl --user enable --now agent-adapter@omp`.
6. `agent_id`, `agent_name`, `journal_dir`, `state_file` and `delegation_dir`
   are no longer in `config/adapter.config.toml`; if you customised them, set
   the matching env variables instead. The module is run as
   `python -m agent_adapter`.

The hub side needs no change: protocol keys, the `omp_session_id` hub column
and the journal field `omp` keep their names (they now mean "runtime-native
session id").

## Tests

```bash
cd agent/local-adapter
.venv/bin/python -m pytest tests/ -v
```
