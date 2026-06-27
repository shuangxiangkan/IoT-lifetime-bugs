"""Discover project-defined resource release wrappers and ownership sinks.

Two dual structural inferences over the project's own functions, each run to a
fixpoint (so wrappers/sinks built on other wrappers/sinks are supported):

- Release wrappers: a function inferred as a deallocator only when a parameter
  is forwarded to a known release operation on every reachable function-exit
  path (``SN_delete_env(z)`` -> ``free(z)``).
- Ownership sinks: a function inferred to take ownership of a pointer parameter
  when that parameter escapes (into a field/global/list) on some path
  (``MQTTAsync_addCommand(conn)`` -> ``ListAppend(queue, conn, ...)``).

File-local ``static`` inferences carry their source-file scope and are never
applied to same-named functions in another translation unit.
"""

from dataclasses import dataclass
import re

from analysis.controlflow import build_function_cfg
from analysis.dataflow import analyze_forward
from analysis.parsing import strip_casts
from iot.calls import arg_at, find_c_calls, simple_name
from iot.semantics import IoTSemantics, ResourceSpec, SinkSpec


Contract = tuple[int, str]


@dataclass(frozen=True)
class _WrapperState:
    """Definitely released parameter contracts plus simple local aliases."""

    released: frozenset[Contract] = frozenset()
    aliases: tuple[tuple[str, str], ...] = ()

    def resolve(self, variable: str) -> str:
        aliases = dict(self.aliases)
        seen = set()
        while variable in aliases and variable not in seen:
            seen.add(variable)
            variable = aliases[variable]
        return variable

    def release(self, contract: Contract) -> "_WrapperState":
        return _WrapperState(self.released | {contract}, self.aliases)

    def assign(self, variable: str, target: str | None) -> "_WrapperState":
        aliases = dict(self.aliases)
        if target is None:
            aliases.pop(variable, None)
        else:
            aliases[variable] = self.resolve(target)
        return _WrapperState(self.released, tuple(sorted(aliases.items())))


def discover_release_wrappers(
    functions: list[dict], semantics: IoTSemantics
) -> list[ResourceSpec]:
    """Infer project-defined deallocators until no new contract is found."""
    discovered: list[ResourceSpec] = []
    seen: set[tuple[str, int, str, str | None]] = set()

    # Each round must discover at least one previously unseen finite contract.
    # Running to convergence handles arbitrary wrapper-chain depth without an
    # unexplained fixed round limit.
    while True:
        added = False
        for func in functions:
            source_file = func.get("_source_file")
            current = semantics.with_release_wrappers(
                discovered, source_file=source_file
            )
            for spec in _discover_function(func, current):
                key = (
                    next(iter(spec.release_apis)),
                    spec.release_arg,
                    spec.kind,
                    spec.scope_file,
                )
                if key in seen:
                    continue
                seen.add(key)
                discovered.append(spec)
                added = True
        if not added:
            return discovered


def _discover_function(
    func: dict, semantics: IoTSemantics
) -> list[ResourceSpec]:
    params = _parameter_names(func.get("parameters", ""))
    name = func.get("name")
    source_bytes = func.get("_source_bytes")
    if not params or not name or source_bytes is None:
        return []

    cfg = build_function_cfg(func, source_bytes)
    param_indexes = {param: index for index, param in enumerate(params) if param}

    def transfer(node, state: _WrapperState) -> _WrapperState:
        if node.kind not in {"declaration", "statement", "if", "return"}:
            return state
        text = node.condition if node.kind == "if" and node.condition else node.text
        text = strip_casts(text)
        for call in find_c_calls(text):
            for spec in semantics.release_specs(call.name):
                state = _apply_release(
                    state, call, spec.release_arg, spec.kind, param_indexes
                )
            for lock in semantics.lock_release_specs(call.name):
                state = _apply_release(
                    state, call, lock.release_arg, lock.kind, param_indexes
                )
        assignment = _simple_assignment(text)
        if assignment is not None:
            variable, target = assignment
            state = state.assign(variable, target)
        return state

    result = analyze_forward(
        cfg,
        _WrapperState(),
        transfer,
        _merge_states,
        max_iterations=max(1000, len(cfg.nodes) * 100),
    )

    exit_contracts: list[frozenset[Contract]] = []
    for node in cfg.nodes:
        if node.id not in result.in_states:
            continue
        if not any(edge.target == cfg.exit_id for edge in cfg.successors(node.id)):
            continue
        state = result.out_states.get(node.id, result.in_states[node.id])
        exit_contracts.append(state.released)
    if not exit_contracts:
        return []

    guaranteed = set.intersection(*(set(items) for items in exit_contracts))
    scope_file = func.get("_source_file") if _is_static(func) else None
    return [
        ResourceSpec(
            kind=kind,
            acquire_apis=frozenset(),
            release_apis=frozenset({name}),
            release_arg=index,
            platform="wrapper",
            scope_file=scope_file,
        )
        for index, kind in sorted(guaranteed)
    ]


def _apply_release(
    state: _WrapperState,
    call,
    release_arg: int,
    kind: str,
    param_indexes: dict[str, int],
) -> _WrapperState:
    variable = arg_at(call, release_arg)
    if not variable:
        return state
    parameter = state.resolve(variable)
    index = param_indexes.get(parameter)
    return state.release((index, kind)) if index is not None else state


def _merge_states(states: list[_WrapperState]) -> _WrapperState:
    if not states:
        return _WrapperState()
    released = set(states[0].released)
    for state in states[1:]:
        released.intersection_update(state.released)

    alias_maps = [dict(state.aliases) for state in states]
    aliases = {}
    for name in set.intersection(*(set(items) for items in alias_maps)):
        targets = {items[name] for items in alias_maps}
        if len(targets) == 1:
            aliases[name] = targets.pop()
    return _WrapperState(frozenset(released), tuple(sorted(aliases.items())))


def _simple_assignment(text: str) -> tuple[str, str | None] | None:
    """Return a simple local assignment, stripping casts from its RHS."""
    text = text.strip().rstrip(";")
    if text.count("=") != 1 or re.search(
        r"==|!=|<=|>=|\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=", text
    ):
        return None
    left, right = text.split("=", 1)
    left_names = re.findall(r"\b[A-Za-z_]\w*\b", left)
    if not left_names or "->" in left or "." in left or "[" in left:
        return None
    variable = left_names[-1]
    return variable, simple_name(strip_casts(right.strip()))


def _is_static(func: dict) -> bool:
    return bool(re.search(r"\bstatic\b", func.get("return_type", "")))


# --- ownership-sink inference (dual of release-wrapper inference) ------------


@dataclass(frozen=True)
class _SinkState:
    """Definitely escaped parameters, aliases, and local container contents."""

    escaped: frozenset[int] = frozenset()
    aliases: tuple[tuple[str, str], ...] = ()
    contained: tuple[tuple[str, frozenset[int]], ...] = ()

    def resolve(self, variable: str) -> str:
        aliases = dict(self.aliases)
        seen = set()
        while variable in aliases and variable not in seen:
            seen.add(variable)
            variable = aliases[variable]
        return variable

    def escape(self, index: int) -> "_SinkState":
        return _SinkState(
            self.escaped | {index}, self.aliases, self.contained
        )

    def escape_container(self, variable: str) -> "_SinkState":
        indexes = dict(self.contained).get(self.resolve(variable), frozenset())
        return _SinkState(
            self.escaped | indexes, self.aliases, self.contained
        )

    def contain(self, variable: str, indexes: frozenset[int]) -> "_SinkState":
        if not indexes:
            return self
        contained = dict(self.contained)
        variable = self.resolve(variable)
        contained[variable] = contained.get(variable, frozenset()) | indexes
        return _SinkState(
            self.escaped,
            self.aliases,
            tuple(sorted(contained.items())),
        )

    def contents(self, variable: str) -> frozenset[int]:
        return dict(self.contained).get(self.resolve(variable), frozenset())

    def assign(self, variable: str, target: str | None) -> "_SinkState":
        aliases = dict(self.aliases)
        if target is None:
            aliases.pop(variable, None)
        else:
            aliases[variable] = self.resolve(target)
        return _SinkState(
            self.escaped, tuple(sorted(aliases.items())), self.contained
        )


def discover_ownership_sinks(
    functions: list[dict], semantics: IoTSemantics
) -> list[SinkSpec]:
    """Infer functions that take ownership of a pointer parameter, to a fixpoint."""
    discovered: list[SinkSpec] = []
    seen: set[tuple[str, int, str | None]] = set()
    while True:
        added = False
        for func in functions:
            source_file = func.get("_source_file")
            current = semantics.augmented(sinks=discovered, source_file=source_file)
            for sink in _discover_function_sinks(func, current):
                key = (sink.name, sink.arg, sink.scope_file)
                if key in seen:
                    continue
                seen.add(key)
                discovered.append(sink)
                added = True
        if not added:
            return discovered


def _discover_function_sinks(func: dict, semantics: IoTSemantics) -> list[SinkSpec]:
    params = _parameter_names(func.get("parameters", ""))
    name = func.get("name")
    source_bytes = func.get("_source_bytes")
    if not params or not name or source_bytes is None:
        return []

    cfg = build_function_cfg(func, source_bytes)
    param_indexes = {param: index for index, param in enumerate(params) if param}
    local_names = {
        local
        for node in cfg.nodes
        if node.kind == "declaration"
        if (local := _declaration_name(node.text)) is not None
    }

    def transfer(node, state: _SinkState) -> _SinkState:
        if node.kind not in {"declaration", "statement", "if", "return"}:
            return state
        text = node.condition if node.kind == "if" and node.condition else node.text
        text = strip_casts(text)

        # A parameter passed to a known ownership sink escapes (recursion).
        for call in find_c_calls(text):
            for sink in semantics.sink_specs(call.name):
                variable = arg_at(call, sink.arg)
                if variable is None:
                    continue
                index = param_indexes.get(state.resolve(variable))
                if index is not None:
                    state = state.escape(index)
                state = state.escape_container(variable)

        store = _store_assignment(text)
        if store is not None:
            left, target = store
            base = _lvalue_base(left)
            if base is not None:
                resolved_base = state.resolve(base)
                resolved_target = state.resolve(target)
                target_index = param_indexes.get(resolved_target)
                target_contents = state.contents(resolved_target)
                destination_escapes = (
                    resolved_base in param_indexes
                    or resolved_base not in local_names
                )
                if destination_escapes:
                    if target_index is not None:
                        state = state.escape(target_index)
                    state = _escape_indexes(state, target_contents)
                else:
                    indexes = target_contents
                    if target_index is not None:
                        indexes |= frozenset({target_index})
                    state = state.contain(resolved_base, indexes)

        assignment = _simple_assignment(text)
        if assignment is not None:
            variable, target = assignment
            state = state.assign(variable, target)
        return state

    result = analyze_forward(
        cfg,
        _SinkState(),
        transfer,
        _merge_sink_states,
        max_iterations=max(1000, len(cfg.nodes) * 100),
    )

    exit_states: list[frozenset[int]] = []
    for node in cfg.nodes:
        if node.id not in result.in_states:
            continue
        if not any(edge.target == cfg.exit_id for edge in cfg.successors(node.id)):
            continue
        state = result.out_states.get(node.id, result.in_states[node.id])
        exit_states.append(state.escaped)
    if not exit_states:
        return []
    escaped = set.intersection(*(set(items) for items in exit_states))
    if not escaped:
        return []

    scope_file = func.get("_source_file") if _is_static(func) else None
    return [
        SinkSpec(name=name, arg=index, scope_file=scope_file)
        for index in sorted(escaped)
    ]


def _merge_sink_states(states: list[_SinkState]) -> _SinkState:
    if not states:
        return _SinkState()
    # A sink contract suppresses a caller leak unconditionally, so only facts
    # true on every predecessor are safe to retain.
    escaped = set(states[0].escaped)
    for state in states[1:]:
        escaped.intersection_update(state.escaped)
    alias_maps = [dict(state.aliases) for state in states]
    aliases = {}
    for key in set.intersection(*(set(items) for items in alias_maps)):
        targets = {items[key] for items in alias_maps}
        if len(targets) == 1:
            aliases[key] = targets.pop()
    contained_maps = [dict(state.contained) for state in states]
    contained = {}
    for variable in set.intersection(*(set(items) for items in contained_maps)):
        indexes = set(contained_maps[0][variable])
        for items in contained_maps[1:]:
            indexes.intersection_update(items[variable])
        if indexes:
            contained[variable] = frozenset(indexes)
    return _SinkState(
        frozenset(escaped),
        tuple(sorted(aliases.items())),
        tuple(sorted(contained.items())),
    )


def _store_assignment(text: str) -> tuple[str, str] | None:
    """Return ``(lvalue, rhs-name)`` for a possible ownership store.

    Whether the destination really escapes is decided separately using the
    function's parameters and locals. This prevents a local array or local
    aggregate field from being mistaken for longer-lived storage.
    """
    text = text.strip().rstrip(";")
    if text.count("=") != 1 or re.search(
        r"==|!=|<=|>=|\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=", text
    ):
        return None
    left, right = text.split("=", 1)
    left = left.strip()
    target = simple_name(strip_casts(right.strip()))
    if target is None:
        return None
    is_structural = (
        "->" in left
        or "." in left
        or "[" in left
        or bool(re.match(r"^\s*\(?\s*\*+\s*[A-Za-z_]\w*\s*\)?$", left))
    )
    # A bare unknown lvalue may be a file-scope/global destination. Plain local
    # aliases are filtered later using ``local_names``.
    if not is_structural and simple_name(left) is None:
        return None
    return left, target


def _lvalue_base(left: str) -> str | None:
    names = re.findall(r"\b[A-Za-z_]\w*\b", left)
    return names[0] if names else None


def _declaration_name(text: str) -> str | None:
    left = text.strip().rstrip(";").split("=", 1)[0]
    names = re.findall(r"\b[A-Za-z_]\w*\b", left)
    return names[-1] if names else None


def _escape_indexes(
    state: _SinkState, indexes: frozenset[int]
) -> _SinkState:
    for index in indexes:
        state = state.escape(index)
    return state


def _parameter_names(params_text: str) -> list[str]:
    """Return ordered names from an ordinary C/C++ parameter list."""
    text = params_text.strip()
    if not text or text == "void":
        return []
    names: list[str] = []
    for piece in _split_top_level(text):
        piece = piece.strip()
        if not piece or piece == "void" or "..." in piece:
            names.append("")
            continue
        piece = re.sub(r"\[[^\]]*\]", "", piece)
        identifiers = re.findall(r"[A-Za-z_]\w*", piece)
        names.append(identifiers[-1] if identifiers else "")
    return names


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(text):
        if ch in "([{<":
            depth += 1
        elif ch in ")]}>":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts
