"""Path-sensitive protocol-order (typestate) checking for IoT objects.

Reports ``invalid_protocol_transition`` candidates: an API used on an object
whose tracked protocol state is not a legal precondition for it -- e.g. using a
client after destroy, publishing before connect, or re-initializing without
teardown. Deliberately conservative for a coarse pre-screen: an object whose
state is unknown (untracked, or disagreed-upon at a CFG join) is never flagged.
"""

from dataclasses import dataclass

from analysis.controlflow import build_function_cfg
from analysis.dataflow import DataFlowResult, analyze_forward
from analysis.parsing import strip_casts
from iot.calls import arg_at, assigned_variable_for_call, find_c_calls
from iot.protocol_state import UNKNOWN, ProtocolState, merge_protocol_states
from iot.semantics import IoTSemantics, ProtocolSpec, Transition


@dataclass(frozen=True)
class ProtocolFinding:
    type: str
    function: str
    line: int
    variable: str
    confidence: str
    detail: str
    state: str
    api_call: str


_NODE_KINDS = {"declaration", "statement", "if", "return"}


def analyze_function_protocol(
    func, source_bytes: bytes, semantics: IoTSemantics
) -> list[ProtocolFinding]:
    """Run typestate analysis for one function; empty when no protocols load."""
    if not semantics.protocols:
        return []
    cfg = build_function_cfg(func, source_bytes)
    proto_states = {id(p): _protocol_states(p) for p in semantics.protocols}

    def transfer(node, state):
        return _transfer(node, state, semantics, proto_states)

    result = analyze_forward(
        cfg,
        ProtocolState(),
        transfer,
        merge_protocol_states,
        max_iterations=max(1000, len(cfg.nodes) * 100),
    )
    return _find_illegal(func["name"], cfg, result, semantics, proto_states)


def _node_text(node) -> str | None:
    if node.kind not in _NODE_KINDS:
        return None
    text = node.condition if node.kind == "if" and node.condition else node.text
    return strip_casts(text)


def _transfer(node, state, semantics, proto_states) -> ProtocolState:
    text = _node_text(node)
    if text is None:
        return state
    for call in find_c_calls(text):
        create = semantics.protocol_create_spec(call.name)
        if create is not None:
            obj = _created_object(text, call, create)
            if obj:
                state = state.set(obj, create.initial_state)
            continue
        for proto, transition in semantics.protocol_transitions(call.name):
            obj = arg_at(call, transition.arg)
            if not obj:
                continue
            current = state.get(obj)
            if current is None or current == UNKNOWN:
                continue
            if current in transition.from_states:
                state = state.set(obj, transition.to_state)
            elif current in proto_states[id(proto)]:
                # Illegal here (reported from in-state in the post-pass); move to
                # UNKNOWN so a single mistake does not cascade into more reports.
                state = state.set(obj, UNKNOWN)
    return state


def _find_illegal(
    function: str,
    cfg,
    result: DataFlowResult,
    semantics: IoTSemantics,
    proto_states,
) -> list[ProtocolFinding]:
    findings: list[ProtocolFinding] = []
    seen: set[tuple[str, int, str]] = set()
    for node in cfg.nodes:
        if node.id not in result.in_states:
            continue
        text = _node_text(node)
        if text is None:
            continue
        in_state = result.in_states[node.id]
        for call in find_c_calls(text):
            pairs = semantics.protocol_transitions(call.name)
            if not pairs:
                continue
            # Aggregate legality per object: relevant if the object's state
            # belongs to a protocol defining this API; legal if some matching
            # transition accepts that state.
            agg: dict[str, tuple[bool, bool, ProtocolSpec, Transition]] = {}
            for proto, transition in pairs:
                obj = arg_at(call, transition.arg)
                if not obj:
                    continue
                current = in_state.get(obj)
                if current is None or current == UNKNOWN:
                    continue
                relevant = current in proto_states[id(proto)]
                legal = current in transition.from_states
                prev = agg.get(obj)
                agg[obj] = (
                    (prev[0] if prev else False) or relevant,
                    (prev[1] if prev else False) or legal,
                    proto,
                    transition,
                )
            for obj, (relevant, legal, proto, transition) in agg.items():
                if not relevant or legal:
                    continue
                key = (obj, node.start_line, call.name)
                if key in seen:
                    continue
                seen.add(key)
                current = in_state.get(obj)
                findings.append(
                    ProtocolFinding(
                        type="invalid_protocol_transition",
                        function=function,
                        line=node.start_line,
                        variable=obj,
                        confidence="medium",
                        detail=(
                            f"{proto.kind} '{obj}' is in state '{current}' when "
                            f"{call.name}() is called, which requires one of "
                            f"{sorted(transition.from_states)}"
                        ),
                        state=current,
                        api_call=call.name,
                    )
                )
    return findings


def _created_object(text: str, call, create: ProtocolSpec) -> str | None:
    if create.create_result == "arg":
        return arg_at(call, create.create_arg)
    return assigned_variable_for_call(text, call)


def _protocol_states(proto: ProtocolSpec) -> frozenset[str]:
    states = {proto.initial_state}
    for transition in proto.transitions:
        states |= transition.from_states
        states.add(transition.to_state)
    return frozenset(states)
