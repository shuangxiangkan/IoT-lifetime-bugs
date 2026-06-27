"""CFG/data-flow based IoT resource lifetime transfer rules.

Ports the path-aware ownership model from the JNI/CPython analyzers onto
data-driven IoT resource specs: acquire binds a resource to a variable, release
clears it, escape/return hand ownership off, and any resource still active at a
function exit on a real CFG path is reported as a candidate leak. IoT-specific
additions over the JNI port: out-parameter acquisition, ``non_negative`` (fd
``< 0``) failure-branch refinement, locks, and path-aware double-release.
"""

from dataclasses import dataclass
import re

from analysis.controlflow import ControlFlowGraph, build_function_cfg
from analysis.dataflow import DataFlowResult, analyze_forward
from analysis.parsing import strip_casts
from iot.calls import (
    CCall,
    arg_at,
    assigned_variable_for_call,
    assignment_lhs_for_call,
    find_c_calls,
    simple_name,
)
from iot.resource_state import (
    ACTIVE,
    DECLARED_KIND,
    RELEASED,
    ResourceState,
    merge_resource_states,
)
from iot.semantics import IoTSemantics, NON_NEGATIVE


@dataclass(frozen=True)
class ResourceFinding:
    """An IoT resource lifetime issue found by path-aware analysis."""

    type: str
    function: str
    line: int
    variable: str
    confidence: str
    detail: str
    acquire_line: int | None = None
    api_call: str | None = None


@dataclass
class ResourceAnalysis:
    """Resource data-flow result for one function."""

    cfg: ControlFlowGraph
    dataflow: DataFlowResult
    findings: list[ResourceFinding]


def analyze_function_resources(
    func, source_bytes: bytes, semantics: IoTSemantics
) -> ResourceAnalysis:
    """Run IoT resource lifetime data-flow analysis for one function."""
    cfg = build_function_cfg(func, source_bytes)

    def transfer(node, state):
        return transfer_resource_node(node, state, semantics)

    result = analyze_forward(
        cfg,
        ResourceState(),
        transfer,
        merge_resource_states,
        edge_transfer=lambda edge, state: refine_resource_edge(
            cfg, edge, state, semantics
        ),
        max_iterations=max(1000, len(cfg.nodes) * 100),
    )
    findings = _find_exit_leaks(func["name"], cfg, result, semantics)
    findings += _find_double_release(func["name"], cfg, result, semantics)
    findings += _find_use_after_release(func["name"], cfg, result, semantics)
    findings += _find_owned_overwrite(func["name"], cfg, result, semantics)
    return ResourceAnalysis(cfg=cfg, dataflow=result, findings=findings)


def transfer_resource_node(
    node, state: ResourceState, semantics: IoTSemantics
) -> ResourceState:
    """Apply IoT resource effects for one CFG node."""
    if node.kind not in {"declaration", "statement", "if", "return"}:
        return state
    text = node.condition if node.kind == "if" and node.condition else node.text
    text = strip_casts(text)
    calls = find_c_calls(text)
    handled = False
    for call in calls:
        new_state = _transfer_call(text, call, node.start_line, state, semantics)
        if new_state is not state:
            handled = True
            state = new_state
    if handled:
        return state
    # No resource call on this node: it may be a plain declaration (record the
    # local so escape detection can tell it apart from a global) or a plain
    # assignment handing a tracked resource off to a field/global (escape).
    if node.kind == "declaration":
        declared = _declared_variable(text)
        if declared and not _is_static_declaration(text):
            return state.note_declared(declared)
    assigned = _assigned_name(text, state)
    if assigned:
        variable, target = assigned
        return state.alias(variable, target)
    escaped = _escaped_assignment(text, state)
    if escaped:
        return state.escape(escaped, line=node.start_line)
    return state


def refine_resource_edge(
    cfg: ControlFlowGraph,
    edge,
    state: ResourceState,
    semantics: IoTSemantics,
) -> ResourceState:
    """Refine state on the failed-acquisition branch of a success check.

    ``non_null`` resources fail with NULL (``if (!p) ...``); ``non_negative``
    resources (POSIX fds) fail with a negative value (``if (fd < 0) ...``). On
    the failure branch the acquire did not happen, so the resource is not a
    leak: drop it by marking it released.
    """
    source = cfg.nodes[edge.source]
    if source.kind != "if" or edge.kind not in {"true", "false"}:
        return state
    condition = source.condition or ""

    null_var, null_edge = _null_check(condition)
    if null_var and edge.kind == null_edge and _is_non_null_resource(null_var, state, semantics):
        return state.release(null_var, line=source.start_line, api="failed-acquire")

    neg_var, neg_edge = _negative_check(condition)
    if neg_var and edge.kind == neg_edge:
        return state.release(neg_var, line=source.start_line, api="failed-acquire")

    for variable, resource in state.active_resources().items():
        if not resource.status_variable or not resource.success_condition:
            continue
        failure_edge = _value_failure_edge(
            condition,
            resource.status_variable,
            resource.success_condition,
        )
        if failure_edge == edge.kind:
            state = state.release(
                variable, line=source.start_line, api="failed-acquire"
            )

    # Out-parameter acquisitions and lock operations bind the resource to an
    # argument, while the call's return value says whether acquisition
    # succeeded. Refine the failed branch so it does not carry a phantom active
    # resource to function exit.
    for call in find_c_calls(condition):
        acquire = semantics.acquire_spec(call.name)
        if acquire is not None and acquire.acquire_result == "arg":
            failure_edge = _call_failure_edge(condition, call, acquire.success)
            var = arg_at(call, acquire.acquire_arg)
            if var and failure_edge == edge.kind:
                state = state.release(
                    var, line=source.start_line, api="failed-acquire"
                )
        lock = semantics.lock_acquire_spec(call.name)
        if lock is not None:
            failure_edge = _call_failure_edge(condition, call, lock.success)
            var = arg_at(call, lock.acquire_arg)
            if var and failure_edge == edge.kind:
                state = state.release(
                    var, line=source.start_line, api="failed-acquire"
                )
    return state


def _is_non_null_resource(
    variable: str, state: ResourceState, semantics: IoTSemantics
) -> bool:
    resource = state.get(variable)
    if resource is None:
        return False
    for spec in semantics.resources:
        if spec.kind == resource.kind:
            return spec.success != NON_NEGATIVE
    return True


def _transfer_call(
    text: str,
    call: CCall,
    line: int,
    state: ResourceState,
    semantics: IoTSemantics,
) -> ResourceState:
    # Resource release first: a name can appear in both tables only by mistake,
    # and releasing a tracked var is the safer default.
    release = _matching_release(call, state, semantics)
    if release is not None:
        var, _spec = release
        return state.release(var, line=line, api=call.name)

    acquire_spec = semantics.acquire_spec(call.name)
    if acquire_spec is not None:
        if acquire_spec.acquire_result == "arg":
            var = arg_at(call, acquire_spec.acquire_arg)
            if var:
                return state.set(
                    var,
                    acquire_spec.kind,
                    line=line,
                    api=call.name,
                    status_variable=assigned_variable_for_call(text, call),
                    success_condition=acquire_spec.success,
                )
        else:
            variable = assigned_variable_for_call(text, call)
            if variable:
                lhs = assignment_lhs_for_call(text, call)
                if lhs is not None and _is_escape_lvalue(lhs, state):
                    # Acquired straight into a field/global/out-param: it escapes
                    # this function and is not tracked as a leakable resource.
                    return state
                return state.set(variable, acquire_spec.kind, line=line, api=call.name)

    lock_release = _matching_lock_release(call, state, semantics)
    if lock_release is not None:
        var, _spec = lock_release
        return state.release(var, line=line, api=call.name)

    lock_acquire = semantics.lock_acquire_spec(call.name)
    if lock_acquire is not None:
        var = arg_at(call, lock_acquire.acquire_arg)
        if var:
            return state.set(
                var,
                lock_acquire.kind,
                line=line,
                api=call.name,
                status_variable=assigned_variable_for_call(text, call),
                success_condition=lock_acquire.success,
            )

    # Ownership sink: handing a tracked resource to a project function that
    # stores it (a command/packet queue, a registry) transfers ownership, so it
    # is no longer this function's responsibility to release.
    for sink in semantics.sink_specs(call.name):
        var = arg_at(call, sink.arg)
        if var and state.get(var) is not None:
            state = state.escape(var, line=line, api=call.name)
    return state


def _matching_release(
    call: CCall,
    state: ResourceState,
    semantics: IoTSemantics,
):
    """Return the release target/spec only when its resource kind matches."""
    for spec in semantics.release_specs(call.name):
        var = arg_at(call, spec.release_arg)
        resource = state.get(var) if var else None
        if resource is not None and resource.kind == spec.kind:
            return var, spec
    return None


def _matching_lock_release(
    call: CCall,
    state: ResourceState,
    semantics: IoTSemantics,
):
    """Return the lock release target/spec only for the matching lock kind."""
    for spec in semantics.lock_release_specs(call.name):
        var = arg_at(call, spec.release_arg)
        resource = state.get(var) if var else None
        if resource is not None and resource.kind == spec.kind:
            return var, spec
    return None


# --- escape / alias / declaration helpers (ported, generalized from JNI) ----


def _is_escape_lvalue(text: str, state: ResourceState) -> bool:
    text = text.strip()
    if _is_static_declaration(text):
        return True
    if _is_pointer_deref_lvalue(text):
        return True
    if "->" in text or "." in text or "[" in text:
        return True
    return simple_name(text) is not None and not state.has(text)


_VALUE_CAST_RE = re.compile(r"^\(\s*[A-Za-z_][\w\s]*\**\s*\)\s*")


def _strip_value_cast(text: str) -> str:
    """Drop a single leading C value cast: ``(int)fd`` -> ``fd``.

    ``strip_casts`` only removes pointer casts written with ``*``; handle/fd
    return idioms like ``ret = (int)fd; return ret;`` use a no-``*`` cast that
    otherwise hides the underlying resource variable from alias tracking.
    """
    return _VALUE_CAST_RE.sub("", text.strip())


def _escaped_assignment(text: str, state: ResourceState) -> str | None:
    text = text.strip().rstrip(";")
    if _has_non_assignment_operator(text):
        return None
    parts = text.split("=")
    if len(parts) < 2:
        return None
    target = simple_name(_strip_value_cast(parts[-1].strip()))
    if target is None:
        return None
    # A chained assignment ``first = last = node`` escapes the resource if any
    # assignee in the chain is a longer-lived location (global/field/out-param).
    if any(_is_escape_lvalue(part.strip(), state) for part in parts[:-1]):
        return target
    return None


def _assigned_name(text: str, state: ResourceState) -> tuple[str, str] | None:
    text = text.strip().rstrip(";")
    if _has_non_assignment_operator(text) or text.count("=") != 1:
        return None
    left, right = text.split("=", 1)
    target = simple_name(_strip_value_cast(right.strip()))
    if target is None:
        return None
    variable = _assigned_variable(left)
    if variable is None:
        return None
    if _is_escape_lvalue(left.strip(), state):
        return None
    if not _looks_like_declaration(left) and not state.has(variable):
        return None
    return variable, target


_DECL_TYPE_RE = re.compile(
    r"\b(?:int|void|char|short|long|unsigned|signed|struct|enum|union|"
    r"FILE|SOCKET|size_t|ssize_t|uint\w*|int\w*)\b"
)


def _declared_variable(text: str) -> str | None:
    text = text.strip().rstrip(";")
    if "=" in text or "(" in text or ")" in text:
        return None
    if "*" not in text and not _DECL_TYPE_RE.search(text):
        return None
    names = re.findall(r"\b[A-Za-z_]\w*\b", text)
    return names[-1] if names else None


def _assigned_variable(text: str) -> str | None:
    if "->" in text or "." in text or "[" in text or _is_pointer_deref_lvalue(text):
        return None
    names = re.findall(r"\b[A-Za-z_]\w*\b", text)
    return names[-1] if names else None


def _looks_like_declaration(text: str) -> bool:
    return "*" in text or bool(_DECL_TYPE_RE.search(text))


def _is_static_declaration(text: str) -> bool:
    return bool(re.search(r"\bstatic\b", text) and _looks_like_declaration(text))


def _is_pointer_deref_lvalue(text: str) -> bool:
    return bool(re.match(r"^\s*\*+\s*[A-Za-z_]\w*\s*$", text)) or bool(
        re.match(r"^\s*\(\s*\*+\s*[A-Za-z_]\w*\s*\)\s*$", text)
    )


def _return_value(text: str) -> str | None:
    match = re.match(r"^return(?:\s+(.+?))?\s*;?$", text.strip())
    if not match or not match.group(1):
        return None
    return strip_casts(match.group(1)).strip()


def _has_non_assignment_operator(text: str) -> bool:
    return bool(
        re.search(r"==|!=|<=|>=|\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=", text)
    )


def _reduce_inline_assign(condition: str) -> str:
    """Rewrite ``(var = <expr>)`` to ``var`` inside a condition.

    Lets the null/negative checks see through the common acquire-and-test
    idiom ``if ((conn = malloc(sizeof(T))) == NULL)`` / ``if ((fd = open(...))
    < 0)``, which otherwise leaves the resource looking active on the failure
    branch. A balanced-paren scan is used (not a regex) so arbitrarily nested
    call arguments like ``malloc(sizeof(T))`` are handled; ``==``/``!=`` etc.
    are not mistaken for an assignment. For a chained assignment
    ``(ptr = buf = malloc(2))`` the *innermost* assignee (``buf``) is kept,
    matching the variable the acquire is tracked under.
    """
    out: list[str] = []
    i, n = 0, len(condition)
    while i < n:
        if condition[i] == "(":
            j = i + 1
            while j < n and condition[j].isspace():
                j += 1
            match = re.match(r"[A-Za-z_]\w*", condition[j:])
            if match:
                name = match.group(0)
                k = j + len(name)
                while k < n and condition[k].isspace():
                    k += 1
                # A single '=' (not ==, <=, >=, !=) marks an assignment.
                if k < n and condition[k] == "=" and condition[k + 1 : k + 2] != "=":
                    # Walk a chain ``a = b = ... = expr`` to the last assignee,
                    # which is the one bound to the acquired resource.
                    name, k = _last_chained_assignee(condition, name, k)
                    depth, p = 1, i + 1
                    quote: str | None = None
                    escaped = False
                    while p < n and depth:
                        ch = condition[p]
                        if quote is not None:
                            if escaped:
                                escaped = False
                            elif ch == "\\":
                                escaped = True
                            elif ch == quote:
                                quote = None
                        elif ch in {'"', "'"}:
                            quote = ch
                        elif ch == "(":
                            depth += 1
                        elif ch == ")":
                            depth -= 1
                        p += 1
                    out.append(name)
                    i = p
                    continue
        out.append(condition[i])
        i += 1
    return "".join(out)


def _last_chained_assignee(condition: str, name: str, equal_pos: int) -> tuple[str, int]:
    """Given an assignment whose first ``=`` is at ``equal_pos``, return the
    last assignee of a ``a = b = ... = expr`` chain and the index just past its
    ``=``. Stops at the first ``=`` not followed by ``<name> =``."""
    n = len(condition)
    pos = equal_pos + 1
    while True:
        p = pos
        while p < n and condition[p].isspace():
            p += 1
        match = re.match(r"[A-Za-z_]\w*", condition[p:])
        if not match:
            break
        q = p + len(match.group(0))
        while q < n and condition[q].isspace():
            q += 1
        if q < n and condition[q] == "=" and condition[q + 1 : q + 2] != "=":
            name = match.group(0)
            pos = q + 1
            continue
        break
    return name, pos


def _null_check(condition: str) -> tuple[str | None, str | None]:
    condition = _unwrap(_reduce_inline_assign(condition))
    for pattern, edge in (
        (r"^([A-Za-z_]\w*)\s*==\s*(?:NULL|0|nullptr)$", "true"),
        (r"^(?:NULL|0|nullptr)\s*==\s*([A-Za-z_]\w*)$", "true"),
        (r"^([A-Za-z_]\w*)\s*!=\s*(?:NULL|0|nullptr)$", "false"),
        (r"^(?:NULL|0|nullptr)\s*!=\s*([A-Za-z_]\w*)$", "false"),
        (r"^!\s*([A-Za-z_]\w*)$", "true"),
        # Bare truthiness ``if (p) { ... free(p); }``: on the false branch the
        # resource is NULL, so it is not a leak. Guarded by _is_non_null_resource
        # in the caller, so an fd (0 is a valid fd) is not wrongly released.
        (r"^([A-Za-z_]\w*)$", "false"),
    ):
        match = re.match(pattern, condition)
        if match:
            return match.group(1), edge
    return None, None


def _negative_check(condition: str) -> tuple[str | None, str | None]:
    """Recognize POSIX fd failure checks. Returns (var, failure_edge_kind)."""
    condition = _unwrap(_reduce_inline_assign(condition))
    for pattern, edge in (
        (r"^([A-Za-z_]\w*)\s*<\s*0$", "true"),
        (r"^([A-Za-z_]\w*)\s*==\s*-\s*1$", "true"),
        (r"^([A-Za-z_]\w*)\s*>=\s*0$", "false"),
        (r"^([A-Za-z_]\w*)\s*!=\s*-\s*1$", "false"),
        (r"^([A-Za-z_]\w*)\s*>\s*-\s*1$", "false"),
    ):
        match = re.match(pattern, condition)
        if match:
            return match.group(1), edge
    return None, None


def _call_failure_edge(
    condition: str,
    call: CCall,
    success: str,
) -> str | None:
    """Return the CFG edge on which a call-based acquisition failed.

    Supported contracts cover the common C/RTOS conventions: zero on success,
    nonzero on success, non-negative on success, and non-NULL on success.
    """
    normalized = re.sub(r"\s+", "", _unwrap(condition))
    call_text = re.sub(r"\s+", "", call.text)
    escaped = re.escape(call_text)

    comparisons = (
        (rf"^{escaped}==0$", "zero", "false"),
        (rf"^0=={escaped}$", "zero", "false"),
        (rf"^{escaped}!=0$", "zero", "true"),
        (rf"^0!={escaped}$", "zero", "true"),
        (rf"^{escaped}==0$", "nonzero", "true"),
        (rf"^0=={escaped}$", "nonzero", "true"),
        (rf"^{escaped}!=0$", "nonzero", "false"),
        (rf"^0!={escaped}$", "nonzero", "false"),
        (rf"^{escaped}<0$", "non_negative", "true"),
        (rf"^{escaped}>=0$", "non_negative", "false"),
        (rf"^{escaped}==NULL$", "non_null", "true"),
        (rf"^NULL=={escaped}$", "non_null", "true"),
        (rf"^{escaped}!=NULL$", "non_null", "false"),
        (rf"^NULL!={escaped}$", "non_null", "false"),
    )
    for pattern, contract, failure_edge in comparisons:
        if success == contract and re.match(pattern, normalized):
            return failure_edge

    if normalized == call_text:
        if success in {"nonzero", "non_null"}:
            return "false"
        if success == "zero":
            return "true"
    if normalized == f"!{call_text}":
        if success in {"nonzero", "non_null"}:
            return "true"
        if success == "zero":
            return "false"
    return None


def _value_failure_edge(
    condition: str,
    variable: str,
    success: str,
) -> str | None:
    """Return the failed CFG edge for a saved call-result variable."""
    normalized = re.sub(r"\s+", "", _unwrap(condition))
    name = re.escape(variable)
    comparisons = (
        (rf"^{name}==0$", "zero", "false"),
        (rf"^0=={name}$", "zero", "false"),
        (rf"^{name}!=0$", "zero", "true"),
        (rf"^0!={name}$", "zero", "true"),
        (rf"^{name}==0$", "nonzero", "true"),
        (rf"^0=={name}$", "nonzero", "true"),
        (rf"^{name}!=0$", "nonzero", "false"),
        (rf"^0!={name}$", "nonzero", "false"),
        (rf"^{name}<0$", "non_negative", "true"),
        (rf"^{name}>=0$", "non_negative", "false"),
        (rf"^{name}==NULL$", "non_null", "true"),
        (rf"^NULL=={name}$", "non_null", "true"),
        (rf"^{name}!=NULL$", "non_null", "false"),
        (rf"^NULL!={name}$", "non_null", "false"),
    )
    for pattern, contract, failure_edge in comparisons:
        if success == contract and re.match(pattern, normalized):
            return failure_edge
    if normalized == variable:
        if success in {"nonzero", "non_null"}:
            return "false"
        if success == "zero":
            return "true"
    if normalized == f"!{variable}":
        if success in {"nonzero", "non_null"}:
            return "true"
        if success == "zero":
            return "false"
    return None


def _unwrap(condition: str) -> str:
    condition = condition.strip()
    while condition.startswith("(") and condition.endswith(")"):
        condition = condition[1:-1].strip()
    return condition


# --- finding producers -------------------------------------------------------


def _find_exit_leaks(
    function: str,
    cfg: ControlFlowGraph,
    result: DataFlowResult,
    semantics: IoTSemantics,
) -> list[ResourceFinding]:
    findings = []
    # One acquisition that leaks on several exit paths is a single bug; collapse
    # by (variable, kind, acquire site), reported once at the first exit reached.
    seen: set[tuple[str, str, int | None]] = set()
    for node in cfg.nodes:
        if node.id not in result.in_states:
            continue
        exit_edges = [
            edge for edge in cfg.successors(node.id) if edge.target == cfg.exit_id
        ]
        if not exit_edges:
            continue
        if node.kind == "return":
            state = result.in_states[node.id]
        else:
            # The state that actually reaches the exit is the node's out-state
            # refined along the edge to exit. Without this an ``if (p) { ...
            # free(p); }`` would look like a leak on the false branch, where p
            # is NULL. With several exit edges, a resource leaks only if it is
            # still active after refinement on some edge.
            out = result.out_states.get(node.id, result.in_states[node.id])
            state = refine_resource_edge(cfg, exit_edges[0], out, semantics)
            for edge in exit_edges[1:]:
                refined = refine_resource_edge(cfg, edge, out, semantics)
                if refined.active_resources():
                    state = refined
                    break
        returned_var = None
        if node.kind == "return":
            returned = _return_value(node.text)
            returned_var = (
                state.resolve(returned)
                if returned and simple_name(returned)
                else None
            )
        for variable, resource in state.active_resources().items():
            if variable == returned_var:
                continue  # returned to caller: ownership transferred, not leaked
            dedupe_key = (variable, resource.kind, resource.line)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            findings.append(
                ResourceFinding(
                    type=_leak_type(resource.kind),
                    function=function,
                    line=node.start_line,
                    variable=variable,
                    confidence="medium",
                    detail=(
                        f"IoT resource '{variable}' ({resource.kind}) may reach "
                        f"function exit at line {node.start_line} without a "
                        "matching release"
                    ),
                    acquire_line=resource.line,
                    api_call=resource.api,
                )
            )
    return findings


def _find_double_release(
    function: str,
    cfg: ControlFlowGraph,
    result: DataFlowResult,
    semantics: IoTSemantics,
) -> list[ResourceFinding]:
    """Report a release whose target is already released on entry to the node."""
    findings = []
    seen: set[tuple[str, int]] = set()
    for node in cfg.nodes:
        if node.kind not in {"statement", "declaration", "if", "return"}:
            continue
        if node.id not in result.in_states:
            continue
        in_state = result.in_states[node.id]
        text = strip_casts(
            node.condition if node.kind == "if" and node.condition else node.text
        )
        for call in find_c_calls(text):
            release = _matching_release(call, in_state, semantics)
            if release is None:
                release = _matching_lock_release(call, in_state, semantics)
            if release is None:
                continue
            var, _spec = release
            resource = in_state.get(var)
            assert resource is not None
            # Only flag a definite double release (released on every path), not a
            # mixed state where one path released and another did not.
            if resource.state != RELEASED:
                continue
            key = (in_state.resolve(var), node.start_line)
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                ResourceFinding(
                    type="double_release",
                    function=function,
                    line=node.start_line,
                    variable=var,
                    confidence="medium",
                    detail=(
                        f"IoT resource '{var}' is released by {call.name}() at line "
                        f"{node.start_line} but was already released"
                    ),
                    acquire_line=resource.line,
                    api_call=call.name,
                )
            )
    return findings


def _find_use_after_release(
    function: str,
    cfg: ControlFlowGraph,
    result: DataFlowResult,
    semantics: IoTSemantics,
) -> list[ResourceFinding]:
    """Report a resource used after it was released (use-after-free / -close).

    A definitely-released resource that is then passed to a (non-releasing)
    call, or dereferenced via ``->``/``[]``, is flagged. Releasing it again is
    ``double_release`` (reported separately), not a use; reassigning it
    (``p = NULL`` / ``p = malloc(...)``) makes it active again on later nodes,
    so it is not flagged.
    """
    findings = []
    seen: set[tuple[str, int]] = set()
    for node in cfg.nodes:
        if node.kind not in {"statement", "declaration", "if", "return"}:
            continue
        if node.id not in result.in_states:
            continue
        in_state = result.in_states[node.id]
        released_canonical = {
            name
            for name, res in in_state.resources
            if res.state == RELEASED and res.transition_api != "failed-acquire"
        }
        if not released_canonical:
            continue
        # Include simple aliases that still resolve to a released resource, so
        # ``q = p; free(p); use(q)`` is not missed.
        released_names = set(released_canonical)
        for alias, _target in in_state.aliases:
            if in_state.resolve(alias) in released_canonical:
                released_names.add(alias)
        text = strip_casts(
            node.condition if node.kind == "if" and node.condition else node.text
        )
        calls = find_c_calls(text)

        # A call that re-releases the variable is double_release, not a use.
        rereleased: set[str] = set()
        for call in calls:
            rel = _matching_release(call, in_state, semantics) or _matching_lock_release(
                call, in_state, semantics
            )
            if rel is not None:
                rereleased.add(in_state.resolve(rel[0]))

        used: dict[str, str] = {}
        for call in calls:
            for raw in call.args:
                arg = _strip_value_cast(raw.strip())
                if arg.startswith("&"):
                    continue  # &p is usually a re-init out-param, not a use
                arg = simple_name(arg)
                if (
                    arg in released_names
                    and in_state.resolve(arg) not in rereleased
                ):
                    used.setdefault(arg, call.name)
        for var in released_names:
            if in_state.resolve(var) in rereleased or var in used:
                continue
            if _is_dereferenced(text, var):
                used[var] = "dereference"
        if node.kind == "return":
            returned = _return_value(text)
            returned = simple_name(_strip_value_cast(returned or ""))
            if (
                returned in released_names
                and in_state.resolve(returned) not in rereleased
            ):
                used[returned] = "return"

        for var, how in used.items():
            resource = in_state.get(var)
            key = (in_state.resolve(var), node.start_line)
            if key in seen:
                continue
            seen.add(key)
            if how == "dereference":
                via = "dereferenced"
            elif how == "return":
                via = "returned after release"
            else:
                via = f"passed to {how}()"
            findings.append(
                ResourceFinding(
                    type="use_after_release",
                    function=function,
                    line=node.start_line,
                    variable=var,
                    confidence="medium",
                    detail=(
                        f"IoT resource '{var}' is {via} at line {node.start_line} "
                        "after it was already released"
                    ),
                    acquire_line=resource.line if resource else None,
                    api_call=None if how == "dereference" else how,
                )
            )
    return findings


def _is_dereferenced(text: str, var: str) -> bool:
    """True when ``var`` is dereferenced (``*var``, ``var->`` or ``var[``)."""
    name = re.escape(var)
    if re.search(rf"\b{name}\s*(?:->|\[)", text):
        return True
    # Require an expression boundary before ``*`` so a pointer declaration
    # such as ``int *p`` is not mistaken for dereferencing p.
    return re.search(rf"(?:^|[=,(;?:])\s*\*+\s*{name}\b", text) is not None


def _find_owned_overwrite(
    function: str,
    cfg: ControlFlowGraph,
    result: DataFlowResult,
    semantics: IoTSemantics,
) -> list[ResourceFinding]:
    """Report an owned resource overwritten by a new acquisition before release.

    ``p = malloc(8); p = malloc(16);`` and ``p = borrowed`` both lose the first
    block. Return-value and out-parameter acquisitions are covered. Only a
    definite ``ACTIVE`` prior value is flagged (not MIXED), so loop back-edge
    reacquisition remains the loop check's job.
    """
    findings = []
    seen: set[tuple[str, int, int | None]] = set()
    for node in cfg.nodes:
        if node.kind not in {"statement", "declaration", "if", "return"}:
            continue
        if node.id not in result.in_states:
            continue
        in_state = result.in_states[node.id]
        text = strip_casts(
            node.condition if node.kind == "if" and node.condition else node.text
        )
        reported: set[str] = set()

        def report(var: str, new_api: str) -> None:
            old = in_state.get(var)
            if old is None or old.state != ACTIVE or old.kind == DECLARED_KIND:
                return
            # A previous iteration reaches the same source line; leave that to
            # acquire_in_loop_without_release instead of double-reporting.
            if old.line is None or old.line >= node.start_line:
                return
            canonical = in_state.resolve(var)
            key = (canonical, node.start_line, old.line)
            if key in seen:
                return
            seen.add(key)
            reported.add(canonical)
            findings.append(
                ResourceFinding(
                    type="owned_overwrite",
                    function=function,
                    line=node.start_line,
                    variable=var,
                    confidence="medium",
                    detail=(
                        f"IoT resource '{var}' ({old.kind}) acquired at line "
                        f"{old.line} is overwritten by {new_api} at line "
                        f"{node.start_line} without being released first"
                    ),
                    acquire_line=old.line,
                    api_call=old.api,
                )
            )

        protected_realloc: set[str] = set()
        for call in find_c_calls(text):
            spec = semantics.acquire_spec(call.name)
            if spec is None:
                continue
            if spec.acquire_result == "arg":
                var = arg_at(call, spec.acquire_arg)
                if var:
                    report(var, f"{call.name}()")
                continue
            var = assigned_variable_for_call(text, call)
            if not var:
                continue
            lhs = assignment_lhs_for_call(text, call)
            if lhs is not None and _is_escape_lvalue(lhs, in_state):
                continue  # acquired into a field/global, not a plain local
            # realloc(p, ...) conditionally replaces p while preserving the old
            # allocation on failure; it needs dedicated semantics and is not an
            # ordinary lost-handle overwrite.
            realloc_arg = arg_at(call, 0) if call.name == "realloc" else None
            if realloc_arg and in_state.resolve(realloc_arg) == in_state.resolve(var):
                protected_realloc.add(in_state.resolve(var))
                continue
            report(var, f"a new {call.name}()")

        plain = _plain_assignment(text)
        if plain is not None:
            var, rhs = plain
            canonical = in_state.resolve(var)
            if canonical not in reported and canonical not in protected_realloc:
                rhs_name = simple_name(_strip_value_cast(rhs))
                if rhs_name is None or in_state.resolve(rhs_name) != canonical:
                    report(var, "a new value")
    return findings


def _plain_assignment(text: str) -> tuple[str, str] | None:
    """Return a plain local ``(lhs, rhs)`` assignment, excluding comparisons."""
    text = text.strip().rstrip(";")
    if text.count("=") != 1 or _has_non_assignment_operator(text):
        return None
    left, right = text.split("=", 1)
    if "->" in left or "." in left or "[" in left or _is_pointer_deref_lvalue(left):
        return None
    variable = _assigned_variable(left)
    return (variable, right.strip()) if variable else None


def _leak_type(kind: str) -> str:
    return _LEAK_TYPE_OVERRIDES.get(kind, f"{kind}_not_released_on_path")


# Populated by the analyzer from the loaded specs so finding types can use the
# friendly per-resource ``leak_type`` (e.g. ``socket_not_closed``).
_LEAK_TYPE_OVERRIDES: dict[str, str] = {}


def set_leak_type_overrides(semantics: IoTSemantics) -> None:
    _LEAK_TYPE_OVERRIDES.clear()
    for spec in semantics.resources:
        if spec.leak_type:
            _LEAK_TYPE_OVERRIDES[spec.kind] = spec.leak_type
    for lock in semantics.locks:
        _LEAK_TYPE_OVERRIDES[lock.kind] = lock.leak_type
