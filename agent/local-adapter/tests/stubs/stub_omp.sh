#!/usr/bin/env bash
# Canned omp --mode=json replacement for tests.
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
echo '{"type":"session","version":3,"id":"stub-omp-1","cwd":"/tmp"}'
if [ -n "${STUB_BIG_LINE:-}" ]; then
  # One NDJSON line far beyond asyncio's 64KiB default stream limit.
  python3 -c 'import json;print(json.dumps({"type":"message_end","message":{"role":"assistant","content":[{"type":"thinking","thinking":"hmm"},{"type":"text","text":"BIG"*40000}]}}))'
  exit 0
fi
if [ -n "${STUB_STREAM:-}" ]; then
  echo '{"type":"turn_start"}'
  echo '{"type":"message_update","assistantMessageEvent":{"type":"thinking_start","contentIndex":0}}'
  echo '{"type":"tool_execution_start","toolCallId":"c1","toolName":"bash","args":{"command":"echo hi"},"intent":"Running echo"}'
  echo '{"type":"tool_execution_end","toolCallId":"c1","toolName":"bash"}'
  echo '{"type":"message_update","assistantMessageEvent":{"type":"text_start","contentIndex":0}}'
  echo '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","contentIndex":0,"delta":"stub "}}'
  echo '{"type":"message_update","assistantMessageEvent":{"type":"text_delta","contentIndex":0,"delta":"stream"}}'
  echo '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"stub stream"}]}}'
  exit 0
fi
echo '{"type":"message_end","message":{"role":"user","content":[{"type":"text","text":"x"}]}}'
if [ -n "${STUB_DELAY:-}" ]; then sleep "${STUB_DELAY}"; fi
echo '{"type":"message_end","message":{"role":"assistant","content":[{"type":"thinking","thinking":"hmm"},{"type":"text","text":"stub "},{"type":"text","text":"reply"}]}}'
