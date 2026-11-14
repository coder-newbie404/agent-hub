"""Run-facing delegation CLI: `python -m agent_adapter.cli <op> [args]`.

A standalone process cannot await the daemon's WSS connection, so it
writes a request file into the delegation drop box and polls for the
result file; the daemon's DropBox watcher does the actual work. The
result JSON goes to stdout in every case. Exit codes: 0 ok, 1
timeout/transport, 2 broker reported an error payload (ok: false).

Standalone by design: stdlib only, no imports from this package —
the drop box file contract is the only interface to the daemon.
"""
import argparse
import json
import os
import pathlib
import sys
import time
import uuid


def _dropbox_dir() -> str:
    # Matches Settings.delegation_dir (config.py); env override identical.
    return os.environ.get("DELEGATION_DIR", "~/.omp-adapter/delegations")


def main(argv=None):
    """Submit one request to the drop box and wait for the result.

    Returns the parsed result payload when the broker answers ok;
    raises SystemExit(1) on timeout and SystemExit(2) when the broker
    reports an error payload.
    """
    parser = argparse.ArgumentParser(prog="agent_adapter.cli")
    # every op waits on the drop box, so every op waits with a deadline
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--timeout", type=float, default=600.0,
                        help="seconds to wait for the result file")
    sub = parser.add_subparsers(dest="op", required=True)
    sub.add_parser("agents", parents=[common])
    p = sub.add_parser("sessions", parents=[common])
    p.add_argument("--agent", required=True)
    p = sub.add_parser("create", parents=[common])
    p.add_argument("--agent", required=True)
    p.add_argument("--description", default="")
    p = sub.add_parser("grant", parents=[common])
    p.add_argument("--session", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--level", choices=["read", "write"], default="read")
    p = sub.add_parser("ask", parents=[common])
    p.add_argument("--session", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--hop", type=int, default=0)
    args = parser.parse_args(argv)
    # command "agents" speaks the drop-box op "directory" (DropBox._dispatch)
    request = {"op": "directory" if args.op == "agents" else args.op}
    if args.op == "sessions":
        request["agent_id"] = args.agent
    elif args.op == "create":
        request.update(agent_id=args.agent, description=args.description)
    elif args.op == "grant":
        request.update(session_id=args.session, agent_id=args.agent,
                       level=args.level)
    elif args.op == "ask":
        request.update(session_id=args.session, text=args.text,
                       hop=args.hop)

    box = pathlib.Path(_dropbox_dir()).expanduser()
    box.mkdir(parents=True, exist_ok=True)
    name = f"req-{uuid.uuid4().hex[:12]}"
    req_path = box / f"{name}.json"
    result_path = box / f"{name}.result.json"
    tmp_req = box / f"{name}.tmp"  # does not match the daemon's req-*.json glob
    tmp_req.write_text(json.dumps(request))
    os.replace(tmp_req, req_path)  # atomic publish: no mid-write consumption

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text())
            except json.JSONDecodeError:
                pass  # result file exists but the writer is mid-write
            else:
                result_path.unlink(missing_ok=True)
                print(json.dumps(result))
                if result.get("ok"):
                    return result
                raise SystemExit(2)
        time.sleep(0.1)
    print(json.dumps({"ok": False, "error": "timeout"}))
    raise SystemExit(1)


if __name__ == "__main__":
    main()  # raises SystemExit on the failure paths
    sys.exit(0)
