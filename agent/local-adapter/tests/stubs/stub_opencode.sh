#!/usr/bin/env bash
# Canned `opencode run --format json` replacement for tests (shapes copied
# from tests/fixtures/opencode, captured from opencode 1.18.29).
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
SID=stub-opencode-1
step_start() {
  echo '{"type":"step_start","timestamp":1,"sessionID":"'$SID'","part":{"id":"prt_1","messageID":"msg_1","sessionID":"'$SID'","type":"step-start"}}'
}
step_finish() {  # $1 = reason
  echo '{"type":"step_finish","timestamp":2,"sessionID":"'$SID'","part":{"id":"prt_9","reason":"'$1'","messageID":"msg_1","sessionID":"'$SID'","type":"step-finish","tokens":{"total":1,"input":1,"output":0,"reasoning":0,"cache":{"write":0,"read":0}},"cost":0}}'
}
text_part() {  # $1 = text
  echo '{"type":"text","timestamp":2,"sessionID":"'$SID'","part":{"id":"prt_2","messageID":"msg_1","sessionID":"'$SID'","type":"text","text":"'$1'","time":{"start":1,"end":2}}}'
}
if [ "${STUB_ERROR_EVENT:-}" = "1" ]; then
  echo '{"type":"error","sessionID":"stub-opencode-1","timestamp":1,"error":{"name":"UnknownError","data":{"message":"Unexpected server error"}}}'
  exit 1
fi
if [ "${STUB_REJECT:-}" = "1" ]; then
  echo '! permission requested: external_directory (/tmp/*); auto-rejecting' >&2
  step_start
  echo '{"type":"tool_use","timestamp":2,"sessionID":"'$SID'","part":{"type":"tool","tool":"write","callID":"c1","state":{"status":"error","input":{"content":"ok\n","filePath":"/tmp/outside.txt"},"error":"The user rejected permission to use this specific tool call.","time":{"start":1,"end":2}},"id":"prt_3","sessionID":"'$SID'","messageID":"msg_1"}}'
  step_finish tool-calls
  exit 0
fi
if [ -n "${STUB_BIG_LINE:-}" ]; then
  # One NDJSON line far beyond asyncio's 64KiB default stream limit.
  step_start
  python3 -c 'import json;print(json.dumps({"type":"text","sessionID":"stub-opencode-1","part":{"type":"text","text":"BIG"*40000}}))'
  step_finish stop
  exit 0
fi
step_start
if [ -n "${STUB_STREAM:-}" ]; then
  echo '{"type":"tool_use","timestamp":2,"sessionID":"'$SID'","part":{"type":"tool","tool":"bash","callID":"c1","state":{"status":"completed","input":{"command":"echo hi"},"output":"hi\n","time":{"start":1,"end":2}},"id":"prt_3","sessionID":"'$SID'","messageID":"msg_1"}}'
  step_finish tool-calls
  step_start
  text_part "stub stream"
  step_finish stop
  exit 0
fi
if [ -n "${STUB_DELAY:-}" ]; then sleep "${STUB_DELAY}"; fi
text_part "stub reply"
step_finish stop
