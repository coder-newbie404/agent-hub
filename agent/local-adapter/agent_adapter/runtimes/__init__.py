"""Runtime registry. A runtime is a module exposing `SPEC: RuntimeSpec`;
built-ins are imported lazily by name so a missing optional module only fails
when that runtime is selected."""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Callable

from agent_adapter.runtimes.base import Runner

_BUILTIN = {
    "omp": "agent_adapter.runtimes.omp",
    "claude": "agent_adapter.runtimes.claude",
    "opencode": "agent_adapter.runtimes.opencode",
}


@dataclass(frozen=True)
class RuntimeSpec:
    name: str
    streaming: bool                 # emits token deltas (drives capabilities)
    policies: frozenset[str]        # policy values this runtime can enforce
    make_runner: Callable[..., Runner]   # (settings) -> Runner
    # optional: native session ids -> human titles (read-only lookups)
    titles: Callable[..., dict[str, str]] | None = None


class UnknownRuntime(ValueError):
    pass


def names() -> list[str]:
    return sorted(_BUILTIN)


def get_runtime(name: str) -> RuntimeSpec:
    module = _BUILTIN.get(name)
    if module is None:
        raise UnknownRuntime(
            f"unknown runtime {name!r}; available: {', '.join(names())}")
    return importlib.import_module(module).SPEC
