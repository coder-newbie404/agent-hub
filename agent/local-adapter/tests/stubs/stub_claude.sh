#!/usr/bin/env bash
# Canned `claude -p --output-format stream-json` replacement for tests.
if [ -n "${STUB_ENV_FILE:-}" ]; then env > "$STUB_ENV_FILE"; fi
if [ "${STUB_FAIL:-}" = "1" ]; then
  echo "stub exploded" >&2
  exit 1
fi
if [ -n "${STUB_ARGS_FILE:-}" ]; then
  printf '%s\n' "$@" > "$STUB_ARGS_FILE"
fi
if [ -n "${STUB_STDERR_FLOOD:-}" ]; then
  for i in $(seq 1 1024); do
    printf 'stderr-flood-%04d-%0512d\n' "$i" 0 >&2
  done
fi
if [ -n "${STUB_TRAP_INT:-}" ]; then
  # Cancel-signal probe: record which signal arrived, then exit like claude.
  trap 'echo int > "$STUB_SIGNAL_FILE"; exit 130' INT
  trap 'echo term > "$STUB_SIGNAL_FILE"; exit 143' TERM
fi
echo '{"type":"system","subtype":"init","session_id":"stub-claude-1","model":"stub","tools":[]}'
if [ -n "${STUB_ERROR_RESULT:-}" ]; then
  echo '{"type":"result","subtype":"error_during_execution","is_error":true,"result":"boom","session_id":"stub-claude-1"}'
  exit 1
fi
if [ -n "${STUB_BIG_LINE:-}" ]; then
  # One NDJSON line far beyond asyncio's 64KiB default stream limit.
  python3 -c 'import json;print(json.dumps({"type":"result","subtype":"success","is_error":False,"result":"BIG"*40000,"session_id":"stub-claude-1"}))'
  exit 0
fi
TEXT="stub reply"
if [ -n "${STUB_STREAM:-}" ]; then
  TEXT="stub stream"
  echo '{"type":"assistant","session_id":"stub-claude-1","message":{"role":"assistant","content":[{"type":"tool_use","id":"tu1","name":"Bash","input":{"command":"echo hi"}}]}}'
  echo '{"type":"user","session_id":"stub-claude-1","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"tu1","content":"hi"}]}}'
fi
echo '{"type":"stream_event","session_id":"stub-claude-1","event":{"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}}'
if [ -n "${STUB_STREAM:-}" ]; then
  echo '{"type":"stream_event","session_id":"stub-claude-1","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"stub "}}}'
  echo '{"type":"stream_event","session_id":"stub-claude-1","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"stream"}}}'
else
  echo '{"type":"stream_event","session_id":"stub-claude-1","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"stub "}}}'
  echo '{"type":"stream_event","session_id":"stub-claude-1","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"reply"}}}'
fi
echo "{\"type\":\"assistant\",\"session_id\":\"stub-claude-1\",\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"text\",\"text\":\"$TEXT\"}]}}"
if [ -n "${STUB_DELAY:-}" ]; then sleep "${STUB_DELAY}"; fi
if [ -n "${STUB_NO_RESULT:-}" ]; then exit 0; fi
echo "{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,\"result\":\"$TEXT\",\"session_id\":\"stub-claude-1\"}"
