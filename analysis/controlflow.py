"""Small intraprocedural control-flow graph builder for C/C++ functions.

The graph is statement-level, not compiler-grade. It is meant for analyzers
that need common C extension control flow: sequential statements, if/else,
while/for/do loops and C++ range-for (with break/continue), return, goto, and
labels. C++ try/catch is modeled as a branch (normal path runs the try body, an
exception path runs a handler). A ``switch`` is treated as an opaque single
node; its body is not expanded.
"""

from dataclasses import dataclass, field

import tree_sitter

from analysis.parsing import get_node_text


@dataclass(frozen=True)
class CFGNode:
    """A statement-level CFG node."""

    id: int
    kind: str
    start_line: int
    end_line: int
    text: str = ""
    label: str | None = None
    goto_label: str | None = None
    condition: str | None = None


@dataclass(frozen=True)
class CFGEdge:
    """A directed CFG edge."""

    source: int
    target: int
    kind: str = "next"


@dataclass
class ControlFlowGraph:
    """Statement-level control-flow graph for one function."""

    function: str
    nodes: list[CFGNode]
    edges: list[CFGEdge]
    entry_id: int
    exit_id: int
    labels: dict[str, int]
    unresolved_gotos: dict[int, str]

    def __post_init__(self) -> None:
        self._successors: dict[int, list[CFGEdge]] = {}
        self._predecessors: dict[int, list[CFGEdge]] = {}
        for edge in self.edges:
            self._successors.setdefault(edge.source, []).append(edge)
            self._predecessors.setdefault(edge.target, []).append(edge)

    def successors(self, node_id: int) -> list[CFGEdge]:
        """Return outgoing edges for a node."""
        return self._successors.get(node_id, [])

    def predecessors(self, node_id: int) -> list[CFGEdge]:
        """Return incoming edges for a node."""
        return self._predecessors.get(node_id, [])


@dataclass
class _LoopContext:
    """Targets for break/continue while building a loop body."""

    continue_id: int
    break_exits: list[tuple[int, str]] = field(default_factory=list)


class _CFGBuilder:
    def __init__(self, func: dict, source_bytes: bytes):
        self.func = func
        self.source_bytes = source_bytes
        self.nodes: list[CFGNode] = []
        self.edges: list[CFGEdge] = []
        self.labels: dict[str, int] = {}
        self.gotos: dict[int, str] = {}
        self.loop_stack: list[_LoopContext] = []
        self.entry_id = self._add_synthetic_node("entry")
        self.exit_id = self._add_synthetic_node("exit")

    def build(self) -> ControlFlowGraph:
        exits = self._build_statement(
            self.func["body_node"],
            [(self.entry_id, "entry")],
        )
        self._connect(exits, self.exit_id)
        self._resolve_gotos()

        unresolved = {
            node_id: target
            for node_id, target in self.gotos.items()
            if target not in self.labels
        }
        return ControlFlowGraph(
            function=self.func["name"],
            nodes=self.nodes,
            edges=self.edges,
            entry_id=self.entry_id,
            exit_id=self.exit_id,
            labels=dict(self.labels),
            unresolved_gotos=unresolved,
        )

    def _add_synthetic_node(self, kind: str) -> int:
        node_id = len(self.nodes)
        line = self.func["start_line"] if kind == "entry" else self.func["end_line"]
        self.nodes.append(
            CFGNode(id=node_id, kind=kind, start_line=line, end_line=line)
        )
        return node_id

    def _add_node(
        self,
        kind: str,
        node: tree_sitter.Node,
        *,
        label: str | None = None,
        goto_label: str | None = None,
        condition: str | None = None,
        text: str | None = None,
    ) -> int:
        node_id = len(self.nodes)
        node_text = text if text is not None else _compact_text(node, self.source_bytes)
        self.nodes.append(
            CFGNode(
                id=node_id,
                kind=kind,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                text=node_text,
                label=label,
                goto_label=goto_label,
                condition=condition,
            )
        )
        return node_id

    def _connect(
        self,
        incoming: list[tuple[int, str]],
        target_id: int,
    ) -> None:
        for source_id, kind in incoming:
            self.edges.append(CFGEdge(source_id, target_id, kind))

    def _build_sequence(
        self,
        statements: list[tree_sitter.Node],
        incoming: list[tuple[int, str]],
    ) -> list[tuple[int, str]]:
        pending = incoming
        for statement in statements:
            pending = self._build_statement(statement, pending)
        return pending

    def _build_statement(
        self,
        statement: tree_sitter.Node,
        incoming: list[tuple[int, str]],
    ) -> list[tuple[int, str]]:
        if statement.type == "compound_statement":
            return self._build_sequence(_direct_statements(statement), incoming)
        if statement.type == "if_statement":
            return self._build_if(statement, incoming)
        if statement.type == "labeled_statement":
            return self._build_label(statement, incoming)
        if statement.type == "goto_statement":
            return self._build_goto(statement, incoming)
        if statement.type == "return_statement":
            return self._build_return(statement, incoming)
        if statement.type in (
            "while_statement",
            "for_statement",
            "do_statement",
            "for_range_loop",
        ):
            return self._build_loop(statement, incoming)
        if statement.type == "try_statement":
            return self._build_try(statement, incoming)
        if statement.type == "break_statement":
            return self._build_break(statement, incoming)
        if statement.type == "continue_statement":
            return self._build_continue(statement, incoming)

        kind = "declaration" if statement.type == "declaration" else "statement"
        node_id = self._add_node(kind, statement)
        self._connect(incoming, node_id)
        return [(node_id, "next")]

    def _build_if(
        self,
        statement: tree_sitter.Node,
        incoming: list[tuple[int, str]],
    ) -> list[tuple[int, str]]:
        condition_node = statement.child_by_field_name("condition")
        condition = (
            get_node_text(condition_node, self.source_bytes)
            if condition_node is not None
            else None
        )
        node_id = self._add_node("if", statement, condition=condition)
        self._connect(incoming, node_id)

        consequence = statement.child_by_field_name("consequence")
        alternative = _if_alternative(statement)

        if consequence is not None:
            true_exits = self._build_statement(consequence, [(node_id, "true")])
        else:
            true_exits = [(node_id, "true")]

        if alternative is not None:
            false_exits = self._build_statement(alternative, [(node_id, "false")])
        else:
            false_exits = [(node_id, "false")]

        return true_exits + false_exits

    def _build_loop(
        self,
        statement: tree_sitter.Node,
        incoming: list[tuple[int, str]],
    ) -> list[tuple[int, str]]:
        condition_node = statement.child_by_field_name("condition")
        condition = (
            get_node_text(condition_node, self.source_bytes)
            if condition_node is not None
            else None
        )
        body = statement.child_by_field_name("body")
        header_id = self._add_node("loop", statement, condition=condition)

        context = _LoopContext(continue_id=header_id)
        self.loop_stack.append(context)

        if statement.type == "do_statement":
            # The body runs once before the condition is tested.
            entry_index = len(self.nodes)
            body_exits = (
                self._build_statement(body, incoming) if body is not None else incoming
            )
            self._connect(body_exits, header_id)
            if len(self.nodes) > entry_index:
                self.edges.append(CFGEdge(header_id, entry_index, "back"))
        else:
            self._connect(incoming, header_id)
            if body is not None:
                body_exits = self._build_statement(body, [(header_id, "true")])
            else:
                body_exits = [(header_id, "true")]
            for source_id, _ in body_exits:
                self.edges.append(CFGEdge(source_id, header_id, "back"))

        self.loop_stack.pop()
        return [(header_id, "false")] + context.break_exits

    def _build_try(
        self,
        statement: tree_sitter.Node,
        incoming: list[tuple[int, str]],
    ) -> list[tuple[int, str]]:
        # Model try/catch as a branch: the normal path runs the try body, while
        # an exception path skips it and runs a handler. This captures the main
        # resource-cleanup hazard -- cleanup in the try body skipped when
        # something throws -- without adding a throw edge after every statement. It is an
        # approximation (a throw mid-body partially executes the try), which
        # suits a candidate-bug heuristic.
        node_id = self._add_node("try", statement)
        self._connect(incoming, node_id)

        body = statement.child_by_field_name("body")
        if body is not None:
            exits = self._build_statement(body, [(node_id, "normal")])
        else:
            exits = [(node_id, "normal")]

        for child in statement.children:
            if child.type != "catch_clause":
                continue
            catch_body = child.child_by_field_name("body")
            if catch_body is not None:
                exits = exits + self._build_statement(
                    catch_body, [(node_id, "exception")]
                )
            else:
                exits = exits + [(node_id, "exception")]

        return exits

    def _build_break(
        self,
        statement: tree_sitter.Node,
        incoming: list[tuple[int, str]],
    ) -> list[tuple[int, str]]:
        node_id = self._add_node("break", statement)
        self._connect(incoming, node_id)
        if self.loop_stack:
            self.loop_stack[-1].break_exits.append((node_id, "break"))
        return []

    def _build_continue(
        self,
        statement: tree_sitter.Node,
        incoming: list[tuple[int, str]],
    ) -> list[tuple[int, str]]:
        node_id = self._add_node("continue", statement)
        self._connect(incoming, node_id)
        if self.loop_stack:
            self.edges.append(
                CFGEdge(node_id, self.loop_stack[-1].continue_id, "continue")
            )
        return []

    def _build_label(
        self,
        statement: tree_sitter.Node,
        incoming: list[tuple[int, str]],
    ) -> list[tuple[int, str]]:
        label_node = statement.child_by_field_name("label")
        if label_node is None:
            for child in statement.children:
                if child.type == "statement_identifier":
                    label_node = child
                    break

        label = (
            get_node_text(label_node, self.source_bytes)
            if label_node is not None
            else None
        )
        node_id = self._add_node(
            "label",
            statement,
            label=label,
            text=f"{label}:" if label else _compact_text(statement, self.source_bytes),
        )
        self._connect(incoming, node_id)
        if label:
            self.labels[label] = node_id

        body = _labeled_body(statement)
        if body is None:
            return [(node_id, "next")]
        return self._build_statement(body, [(node_id, "next")])

    def _build_goto(
        self,
        statement: tree_sitter.Node,
        incoming: list[tuple[int, str]],
    ) -> list[tuple[int, str]]:
        target = _goto_target(statement, self.source_bytes)
        node_id = self._add_node("goto", statement, goto_label=target)
        self._connect(incoming, node_id)
        if target:
            self.gotos[node_id] = target
        return []

    def _build_return(
        self,
        statement: tree_sitter.Node,
        incoming: list[tuple[int, str]],
    ) -> list[tuple[int, str]]:
        node_id = self._add_node("return", statement)
        self._connect(incoming, node_id)
        self.edges.append(CFGEdge(node_id, self.exit_id, "return"))
        return []

    def _resolve_gotos(self) -> None:
        for source_id, label in self.gotos.items():
            target_id = self.labels.get(label)
            if target_id is not None:
                self.edges.append(CFGEdge(source_id, target_id, "goto"))


def build_function_cfg(func: dict, source_bytes: bytes) -> ControlFlowGraph:
    """Build a small statement-level CFG for an extracted function."""
    return _CFGBuilder(func, source_bytes).build()


def _direct_statements(node: tree_sitter.Node) -> list[tree_sitter.Node]:
    return [
        child
        for child in node.children
        if child.is_named and child.type != "comment"
    ]


def _if_alternative(statement: tree_sitter.Node) -> tree_sitter.Node | None:
    alternative = statement.child_by_field_name("alternative")
    if alternative is not None and alternative.type != "else_clause":
        return alternative

    else_clause = alternative
    if else_clause is None:
        for child in statement.children:
            if child.type == "else_clause":
                else_clause = child
                break
    if else_clause is None:
        return None

    for child in else_clause.children:
        if child.is_named and child.type != "comment":
            return child
    return None


def _labeled_body(statement: tree_sitter.Node) -> tree_sitter.Node | None:
    body = statement.child_by_field_name("body")
    if body is not None:
        return body
    for child in statement.children:
        if child.is_named and child.type != "statement_identifier":
            return child
    return None


def _goto_target(statement: tree_sitter.Node, source_bytes: bytes) -> str | None:
    label = statement.child_by_field_name("label")
    if label is not None:
        return get_node_text(label, source_bytes)
    for child in statement.children:
        if child.type == "statement_identifier":
            return get_node_text(child, source_bytes)
    return None


def _compact_text(node: tree_sitter.Node, source_bytes: bytes) -> str:
    return " ".join(get_node_text(node, source_bytes).split())
