#!/usr/bin/env bash
# Minimal generic JSONL agent for base-runner tests (STUB_* contract).
if [ -n "${STUB_ENV_FILE:-}" ]; then env > "$STUB_ENV_FILE"; fi
if [ -n "${STUB_ARGS_FILE:-}" ]; then printf '%s\n' "$@" > "$STUB_ARGS_FILE"; fi
# STUB_TRAP_SIGNAL_FILE=<f>: trap TERM/INT, write the signal name to <f> and
# exit; STUB_IGNORE_SIGNALS=1 ignores both instead (only SIGKILL ends it).
# Either way, announce readiness with a delta once the traps are armed.
if [ -n "${STUB_TRAP_SIGNAL_FILE:-}" ]; then
  trap 'echo TERM > "$STUB_TRAP_SIGNAL_FILE"; exit 0' TERM
  trap 'echo INT > "$STUB_TRAP_SIGNAL_FILE"; exit 0' INT
fi
if [ "${STUB_IGNORE_SIGNALS:-}" = "1" ]; then trap '' TERM INT; fi
if [ -n "${STUB_TRAP_SIGNAL_FILE:-}" ] || [ "${STUB_IGNORE_SIGNALS:-}" = "1" ]; then
  echo '{"type":"delta","text":"armed"}'
fi
echo '{"type":"session","id":"stub-generic-1"}'
if [ "${STUB_FAIL:-}" = "1" ]; then
  echo '{"type":"error","message":"driver says boom"}'
  echo "stub exploded" >&2
  exit 1
fi
if [ -n "${STUB_DELAY:-}" ]; then sleep "${STUB_DELAY}" & wait $!; fi
echo '{"type":"delta","text":"stub "}'
echo '{"type":"delta","text":"reply"}'
echo '{"type":"reply","text":"stub reply"}'
