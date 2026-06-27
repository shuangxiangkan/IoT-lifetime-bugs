"""Small forward data-flow solver for CFG-based function analyses."""

from collections import deque
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from analysis.controlflow import CFGEdge, CFGNode, ControlFlowGraph


State = TypeVar("State")
Transfer = Callable[[CFGNode, State], State]
Merge = Callable[[list[State]], State]
EdgeTransfer = Callable[[CFGEdge, State], State]


@dataclass
class DataFlowResult(Generic[State]):
    """Input and output states for each reached CFG node."""

    in_states: dict[int, State]
    out_states: dict[int, State]
    iterations: int


def analyze_forward(
    cfg: ControlFlowGraph,
    initial_state: State,
    transfer: Transfer[State],
    merge: Merge[State],
    edge_transfer: EdgeTransfer[State] | None = None,
    max_iterations: int = 1000,
) -> DataFlowResult[State]:
    """Run a forward data-flow analysis to a fixed point.

    ``transfer`` updates state for a node. ``merge`` combines predecessor
    states. ``edge_transfer`` can refine state along true/false/goto/back edges.
    States are compared with ``==``; callers should use immutable states or
    return fresh mutable objects from callbacks.
    """
    in_states: dict[int, State] = {cfg.entry_id: initial_state}
    out_states: dict[int, State] = {}
    worklist = deque([cfg.entry_id])
    queued = {cfg.entry_id}
    iterations = 0

    while worklist:
        if iterations >= max_iterations:
            raise RuntimeError(
                f"data-flow analysis of {cfg.function!r} did not converge "
                f"within {max_iterations} iterations"
            )
        iterations += 1

        node_id = worklist.popleft()
        queued.discard(node_id)
        node = cfg.nodes[node_id]
        in_state = in_states[node_id]
        out_state = transfer(node, in_state)
        if out_states.get(node_id) == out_state:
            continue
        out_states[node_id] = out_state

        # Recompute each distinct successor's input by merging all of its
        # predecessor out-states. Dedupe targets so a node with two edges to
        # the same successor (e.g. an empty if) is not merged twice.
        for target in dict.fromkeys(
            edge.target for edge in cfg.successors(node_id)
        ):
            pred_states = [
                out_states[pred.source]
                if edge_transfer is None
                else edge_transfer(pred, out_states[pred.source])
                for pred in cfg.predecessors(target)
                if pred.source in out_states
            ]
            merged = merge(pred_states)
            if in_states.get(target) != merged:
                in_states[target] = merged
                if target not in queued:
                    worklist.append(target)
                    queued.add(target)

    return DataFlowResult(
        in_states=in_states,
        out_states=out_states,
        iterations=iterations,
    )
