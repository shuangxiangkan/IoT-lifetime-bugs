#!/usr/bin/env python3
"""Tree-sitter parsing utilities for general C/C++ source analysis.

This module provides structured access to C/C++ source code via Tree-sitter.
It intentionally avoids project-specific API or framework semantics;
callers can layer domain-specific classification on top of the extracted
functions, declarations, calls, assignments, returns, and struct members.

Requires: pip install tree-sitter tree-sitter-c tree-sitter-cpp

tree-sitter-cpp is a standard dependency (see requirements.txt), so C++ files
are parsed by default. If it is missing the layer degrades gracefully: C++
sources are skipped with a warning rather than mis-parsed with the C grammar.
"""

import json
import re
import sys
from pathlib import Path

try:
    import tree_sitter
    import tree_sitter_c
except ImportError:
    print(
        json.dumps(
            {
                "error": "tree-sitter not installed",
                "install": "pip install tree-sitter tree-sitter-c",
            }
        )
    )
    sys.exit(1)

# Initialize the C parser once at module level.
C_LANGUAGE = tree_sitter.Language(tree_sitter_c.language())
_parser = tree_sitter.Parser(C_LANGUAGE)

# C++ support (optional).
_CPP_AVAILABLE = False
_cpp_parser: tree_sitter.Parser | None = None

try:
    import tree_sitter_cpp

    _CPP_AVAILABLE = True
except ImportError:
    pass

C_EXTENSIONS = frozenset({".c", ".h"})
CPP_EXTENSIONS = frozenset({".cpp", ".cxx", ".cc", ".hpp"})
ALL_SOURCE_EXTENSIONS = C_EXTENSIONS | CPP_EXTENSIONS


def is_cpp_available() -> bool:
    """Check if tree-sitter-cpp is installed."""
    return _CPP_AVAILABLE


def _get_cpp_parser() -> tree_sitter.Parser:
    """Lazily initialize and return the C++ parser."""
    global _cpp_parser
    if _cpp_parser is None:
        if not _CPP_AVAILABLE:
            raise ImportError(
                "tree-sitter-cpp not installed: pip install tree-sitter-cpp"
            )
        cpp_language = tree_sitter.Language(tree_sitter_cpp.language())
        _cpp_parser = tree_sitter.Parser(cpp_language)
    return _cpp_parser


def get_parser_for_file(filepath: Path) -> tree_sitter.Parser:
    """Return the appropriate parser for a file based on its extension."""
    if filepath.suffix in CPP_EXTENSIONS:
        return _get_cpp_parser()
    return _parser


def parse_bytes_for_file(source_bytes: bytes, filepath: Path) -> tree_sitter.Tree:
    """Parse source bytes using the parser appropriate for the file type."""
    parser = get_parser_for_file(filepath)
    return parser.parse(source_bytes)


def parse_file(path: Path) -> tree_sitter.Tree:
    """Parse a source file and return the Tree-sitter syntax tree."""
    source_bytes = path.read_bytes()
    return parse_bytes_for_file(source_bytes, path)


def parse_string(source: str) -> tree_sitter.Tree:
    """Parse a C source string and return the Tree-sitter syntax tree."""
    return _parser.parse(source.encode("utf-8"))


def parse_bytes(source_bytes: bytes) -> tree_sitter.Tree:
    """Parse C source from bytes already in memory."""
    return _parser.parse(source_bytes)


def get_node_text(node: tree_sitter.Node, source_bytes: bytes) -> str:
    """Get the source text for a tree-sitter node."""
    return source_bytes[node.start_byte : node.end_byte].decode(
        "utf-8", errors="replace"
    )


def walk_descendants(node: tree_sitter.Node, type_filter: str | None = None):
    """Yield all descendant nodes, optionally filtered by node type.

    Common type names: 'call_expression', 'return_statement',
    'if_statement', 'goto_statement', 'declaration', 'assignment_expression',
    'binary_expression', 'identifier', 'string_literal'
    """
    cursor = node.walk()
    visited = False
    while True:
        if not visited:
            current = cursor.node
            if type_filter is None or current.type == type_filter:
                yield current
            if cursor.goto_first_child():
                visited = False
                continue
        if cursor.goto_next_sibling():
            visited = False
            continue
        if cursor.goto_parent():
            visited = True
            continue
        break


def get_declarator_name(node: tree_sitter.Node, source_bytes: bytes) -> str | None:
    """Extract the identifier name from a declarator, handling pointers and arrays."""
    if node.type in ("identifier", "field_identifier"):
        return get_node_text(node, source_bytes)
    if node.type == "pointer_declarator":
        decl = node.child_by_field_name("declarator")
        if decl:
            return get_declarator_name(decl, source_bytes)
    if node.type == "array_declarator":
        decl = node.child_by_field_name("declarator")
        if decl:
            return get_declarator_name(decl, source_bytes)
    if node.type == "parenthesized_declarator":
        for child in node.children:
            name = get_declarator_name(child, source_bytes)
            if name:
                return name
    if node.type == "function_declarator":
        decl = node.child_by_field_name("declarator")
        if decl:
            return get_declarator_name(decl, source_bytes)
    for child in node.children:
        if child.type in ("identifier", "field_identifier"):
            return get_node_text(child, source_bytes)
    return None


def _get_function_declarator(node: tree_sitter.Node) -> tree_sitter.Node | None:
    """Find the function_declarator within a declarator tree."""
    if node.type == "function_declarator":
        return node
    for child in node.children:
        result = _get_function_declarator(child)
        if result:
            return result
    return None


# Node types whose bodies host function definitions at (effectively) file
# scope: extern "C" {}, namespaces, and C++ class/struct bodies (inline
# methods). Descending these keeps C behavior unchanged because a C struct body
# never contains a function_definition.
_FUNCTION_HOST_TYPES = frozenset(
    {
        "linkage_specification",
        "namespace_definition",
        "class_specifier",
        "struct_specifier",
    }
)


def _collect_function_definitions(
    nodes, out: list[tree_sitter.Node]
) -> None:
    """Collect function_definition nodes from a list of sibling nodes.

    Recurses into extern "C", namespace, and C++ class/struct bodies so that
    inline methods (including those nested inside a namespace) are included.
    """
    for node in nodes:
        if node.type == "function_definition":
            out.append(node)
        elif node.type in _FUNCTION_HOST_TYPES:
            body = node.child_by_field_name("body")
            if body is not None and body.type == "function_definition":
                out.append(body)
            else:
                children = body.children if body is not None else node.children
                _collect_function_definitions(children, out)


def extract_functions(tree: tree_sitter.Tree, source_bytes: bytes) -> list[dict]:
    """Extract all function definitions from a parse tree.

    Returns list of dicts with keys:
      - name: str (function name)
      - return_type: str
      - parameters: str (raw parameter text)
      - body: str (function body text, excluding braces)
      - body_node: tree_sitter.Node (the compound_statement node)
      - start_line: int (1-indexed)
      - end_line: int (1-indexed)
      - start_byte: int
      - end_byte: int
    """
    functions = []
    root = tree.root_node

    # Collect function definitions at file scope, descending into extern "C",
    # namespace, and C++ class/struct bodies (which wrap inline methods).
    top_nodes: list[tree_sitter.Node] = []
    _collect_function_definitions(root.children, top_nodes)

    for node in top_nodes:
        if node.type != "function_definition":
            continue

        declarator = node.child_by_field_name("declarator")
        body_node = node.child_by_field_name("body")
        if not declarator or not body_node:
            continue

        # Get the return type: everything before the declarator.
        return_type_parts = []
        for child in node.children:
            if child == declarator:
                break
            if child.type not in ("comment",):
                return_type_parts.append(get_node_text(child, source_bytes))
        return_type = " ".join(return_type_parts).strip()

        # Find the function_declarator to get name and params.
        func_decl = _get_function_declarator(declarator)
        if not func_decl:
            continue

        name_node = func_decl.child_by_field_name("declarator")
        params_node = func_decl.child_by_field_name("parameters")

        if not name_node:
            continue

        func_name = get_declarator_name(name_node, source_bytes)
        if not func_name:
            continue

        params_text = ""
        if params_node:
            params_text = get_node_text(params_node, source_bytes)
            # Strip outer parentheses.
            if params_text.startswith("(") and params_text.endswith(")"):
                params_text = params_text[1:-1].strip()

        # Body text: strip outer braces.
        body_text = get_node_text(body_node, source_bytes)
        if body_text.startswith("{") and body_text.endswith("}"):
            body_text = body_text[1:-1]

        functions.append(
            {
                "name": func_name,
                "return_type": return_type,
                "parameters": params_text,
                "body": body_text,
                "body_node": body_node,
                "start_line": node.start_point[0] + 1,
                "end_line": node.end_point[0] + 1,
                "start_byte": node.start_byte,
                "end_byte": node.end_byte,
            }
        )

    return functions


def extract_struct_initializers(
    tree: tree_sitter.Tree, source_bytes: bytes, type_name: str
) -> list[dict]:
    """Find static struct initializers for a given type name.

    e.g., extract_struct_initializers(tree, source, "HandlerDef") finds:
      static HandlerDef handlers[] = { ... };

    Returns list of dicts with keys:
      - variable_name: str
      - type_name: str
      - is_array: bool
      - initializer_text: str (the { ... } content)
      - initializer_node: tree_sitter.Node
      - start_line: int
      - end_line: int
    """
    results = []
    root = tree.root_node

    for node in root.children:
        if node.type != "declaration":
            continue

        decl_text = get_node_text(node, source_bytes)
        # Check if the type name appears in the declaration.
        if type_name not in decl_text:
            continue

        # Look for the type specifier.
        type_node = node.child_by_field_name("type")
        if type_node:
            type_text = get_node_text(type_node, source_bytes)
            if type_name not in type_text:
                # Also check if it's "struct type_name"
                found = False
                for desc in walk_descendants(type_node):
                    if (
                        desc.type == "type_identifier"
                        and get_node_text(desc, source_bytes) == type_name
                    ):
                        found = True
                        break
                if not found:
                    continue

        # Find init_declarator children to get variable name and initializer.
        for child in node.children:
            if child.type != "init_declarator":
                continue

            declarator = child.child_by_field_name("declarator")
            value = child.child_by_field_name("value")
            if not declarator or not value:
                continue

            var_name = get_declarator_name(declarator, source_bytes)
            if not var_name:
                continue

            is_array = "array_declarator" in get_node_text(
                declarator, source_bytes
            ) or "[" in get_node_text(declarator, source_bytes)

            init_text = get_node_text(value, source_bytes)

            results.append(
                {
                    "variable_name": var_name,
                    "type_name": type_name,
                    "is_array": is_array,
                    "initializer_text": init_text,
                    "initializer_node": value,
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                }
            )

    return results


def extract_static_declarations(
    tree: tree_sitter.Tree, source_bytes: bytes
) -> list[dict]:
    """Find all file-scope static variable declarations.

    Returns list of dicts with keys:
      - name: str (variable name)
      - type: str (full type including qualifiers, e.g., "static const char *")
      - is_const: bool
      - is_pointer: bool
      - initializer: str | None
      - start_line: int
    """
    return extract_global_declarations(tree, source_bytes, storage_class="static")


def extract_global_declarations(
    tree: tree_sitter.Tree,
    source_bytes: bytes,
    storage_class: str | None = None,
) -> list[dict]:
    """Find file-scope variable declarations.

    If storage_class is provided, only declarations with that storage class are
    returned, for example ``"static"`` or ``"extern"``.

    Returns list of dicts with keys:
      - name: str (variable name)
      - type: str (full type including qualifiers)
      - storage_class: str | None
      - is_const: bool
      - is_pointer: bool
      - initializer: str | None
      - start_line: int
    """
    results = []
    root = tree.root_node

    for node in root.children:
        if node.type != "declaration":
            continue

        decl_text = get_node_text(node, source_bytes)
        found_storage_class = None
        for child in node.children:
            if child.type == "storage_class_specifier":
                found_storage_class = get_node_text(child, source_bytes)
                break

        if storage_class is not None:
            # Prefer AST storage-class detection, but keep the text prefix check
            # for parser-tolerant cases around macros or unusual declarations.
            has_requested_storage = found_storage_class == storage_class
            if not has_requested_storage and decl_text.lstrip().startswith(
                storage_class
            ):
                has_requested_storage = True
                found_storage_class = storage_class
            if not has_requested_storage:
                continue
        elif found_storage_class is None:
            for child in node.children:
                if (
                    child.type == "storage_class_specifier"
                    and get_node_text(child, source_bytes)
                ):
                    found_storage_class = get_node_text(child, source_bytes)
                    break

        # Skip function declarations (those have function_declarator).
        is_func_decl = False
        for desc in walk_descendants(node, "function_declarator"):
            is_func_decl = True
            break
        if is_func_decl:
            continue

        # Get type info.
        is_const = "const" in decl_text.split("=")[0]

        # Find each declared variable.
        for child in node.children:
            if child.type == "init_declarator":
                declarator = child.child_by_field_name("declarator")
                value = child.child_by_field_name("value")
                if not declarator:
                    continue
                var_name = get_declarator_name(declarator, source_bytes)
                if not var_name:
                    continue
                decl_part = decl_text.split("=")[0].strip().rstrip(";").strip()
                is_pointer = "*" in decl_part
                init_text = get_node_text(value, source_bytes) if value else None
                results.append(
                    {
                        "name": var_name,
                        "type": decl_part.rsplit(var_name, 1)[0].strip()
                        if var_name in decl_part
                        else decl_part,
                        "storage_class": found_storage_class,
                        "is_const": is_const,
                        "is_pointer": is_pointer,
                        "initializer": init_text,
                        "start_line": node.start_point[0] + 1,
                    }
                )
            elif child.type in ("identifier", "pointer_declarator", "array_declarator"):
                # Declaration without initializer.
                var_name = get_declarator_name(child, source_bytes)
                if not var_name:
                    continue
                decl_part = decl_text.strip().rstrip(";").strip()
                is_pointer = "*" in decl_part
                results.append(
                    {
                        "name": var_name,
                        "type": decl_part.rsplit(var_name, 1)[0].strip()
                        if var_name in decl_part
                        else decl_part,
                        "storage_class": found_storage_class,
                        "is_const": is_const,
                        "is_pointer": is_pointer,
                        "initializer": None,
                        "start_line": node.start_point[0] + 1,
                    }
                )

    return results


def find_calls_in_scope(
    node: tree_sitter.Node, source_bytes: bytes, api_names: set[str] | None = None
) -> list[dict]:
    """Find all function calls within a given AST node (typically a function body).

    If api_names is provided, only return calls to those functions.

    Returns list of dicts with keys:
      - function_name: str
      - arguments_text: str
      - node: tree_sitter.Node
      - start_line: int
      - start_byte: int
    """
    results = []
    for call_node in walk_descendants(node, "call_expression"):
        func_node = call_node.child_by_field_name("function")
        args_node = call_node.child_by_field_name("arguments")
        if not func_node:
            continue

        func_name = get_node_text(func_node, source_bytes)
        if api_names is not None and func_name not in api_names:
            continue

        args_text = ""
        if args_node:
            args_text = get_node_text(args_node, source_bytes)
            if args_text.startswith("(") and args_text.endswith(")"):
                args_text = args_text[1:-1].strip()

        results.append(
            {
                "function_name": func_name,
                "arguments_text": args_text,
                "node": call_node,
                "start_line": call_node.start_point[0] + 1,
                "start_byte": call_node.start_byte,
            }
        )

    return results


def find_assignments_in_scope(
    node: tree_sitter.Node, source_bytes: bytes, var_name: str | None = None
) -> list[dict]:
    """Find variable assignments within a scope.

    If var_name is provided, only return assignments to that variable.

    Returns list of dicts with keys:
      - variable: str
      - value_text: str
      - value_node: tree_sitter.Node
      - is_declaration: bool (part of a declaration vs standalone assignment)
      - start_line: int
    """
    results = []

    # Find standalone assignments (assignment_expression).
    for assign_node in walk_descendants(node, "assignment_expression"):
        left = assign_node.child_by_field_name("left")
        right = assign_node.child_by_field_name("right")
        if not left or not right:
            continue
        assigned_var = get_node_text(left, source_bytes)
        if var_name is not None and assigned_var != var_name:
            continue
        results.append(
            {
                "variable": assigned_var,
                "value_text": get_node_text(right, source_bytes),
                "value_node": right,
                "is_declaration": False,
                "start_line": assign_node.start_point[0] + 1,
            }
        )

    # Find declaration-initializations (init_declarator inside declarations).
    for decl_node in walk_descendants(node, "init_declarator"):
        declarator = decl_node.child_by_field_name("declarator")
        value = decl_node.child_by_field_name("value")
        if not declarator or not value:
            continue
        declared_var = get_declarator_name(declarator, source_bytes)
        if not declared_var:
            continue
        if var_name is not None and declared_var != var_name:
            continue
        results.append(
            {
                "variable": declared_var,
                "value_text": get_node_text(value, source_bytes),
                "value_node": value,
                "is_declaration": True,
                "start_line": decl_node.start_point[0] + 1,
            }
        )

    return results


def find_assigned_variable(call_node, source_bytes: bytes) -> str | None:
    """Find the variable a call expression result is assigned to.

    Handles direct assignment and declaration initialization, for example:

    ``x = make_value();``
    ``Value *x = make_value();``

    Macro wrappers around assignments are skipped when the wrapper name is
    all-uppercase, which keeps this useful for C codebases that use assertion
    or error-handling macros.
    """
    node = call_node.parent
    while node:
        if node.type == "init_declarator":
            decl = node.child_by_field_name("declarator")
            if decl:
                return get_declarator_name(decl, source_bytes)
        if node.type == "assignment_expression":
            left = node.child_by_field_name("left")
            if left:
                return get_node_text(left, source_bytes)
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            if func and get_node_text(func, source_bytes).isupper():
                node = node.parent
                continue
        if node.type in ("expression_statement", "declaration", "compound_statement"):
            break
        node = node.parent
    return None


def find_return_statements(node: tree_sitter.Node, source_bytes: bytes) -> list[dict]:
    """Find all return statements within a scope.

    Returns list of dicts with keys:
      - value_text: str | None (None for bare 'return;')
      - node: tree_sitter.Node
      - start_line: int
    """
    results = []
    for ret_node in walk_descendants(node, "return_statement"):
        # A return statement's children: 'return' keyword, optional expression, ';'
        value_text = None
        for child in ret_node.children:
            if child.type not in ("return", ";", "comment"):
                value_text = get_node_text(child, source_bytes)
                break
        results.append(
            {
                "value_text": value_text,
                "node": ret_node,
                "start_line": ret_node.start_point[0] + 1,
            }
        )
    return results


def find_struct_members(
    tree: tree_sitter.Tree, source_bytes: bytes, struct_name: str
) -> list[dict]:
    """Find members of a named struct definition.

    Returns list of dicts with keys:
      - name: str
      - type: str
      - is_pointer: bool
      - start_line: int
    """
    results = []

    # Look for struct definitions in type_definition or struct_specifier nodes.
    for node in walk_descendants(tree.root_node, "struct_specifier"):
        # Check if this struct has a name matching or a typedef name matching.
        name_node = node.child_by_field_name("name")
        struct_ident = get_node_text(name_node, source_bytes) if name_node else None

        # Check if this struct is inside a typedef with the target name.
        parent = node.parent
        is_match = False
        if struct_ident == struct_name:
            is_match = True
        elif parent and parent.type == "type_definition":
            # Check the typedef name.
            type_def_text = get_node_text(parent, source_bytes)
            if struct_name in type_def_text:
                # Find the declarator of the typedef.
                for child in parent.children:
                    if (
                        child.type == "type_identifier"
                        and get_node_text(child, source_bytes) == struct_name
                    ):
                        is_match = True
                        break

        if not is_match:
            continue

        # Find the field_declaration_list (body).
        body = node.child_by_field_name("body")
        if not body:
            continue

        for field in body.children:
            if field.type != "field_declaration":
                continue
            # Find field name from the declarator.
            declarator = field.child_by_field_name("declarator")
            if not declarator:
                continue
            field_name = get_declarator_name(declarator, source_bytes)
            if not field_name:
                continue

            # Get the type.
            type_parts = []
            for child in field.children:
                if child == declarator or child.type == ";":
                    break
                type_parts.append(get_node_text(child, source_bytes))
            field_type = " ".join(type_parts).strip()
            if "*" in get_node_text(declarator, source_bytes):
                field_type += " *"

            is_pointer = "*" in field_type

            results.append(
                {
                    "name": field_name,
                    "type": field_type,
                    "is_pointer": is_pointer,
                    "start_line": field.start_point[0] + 1,
                }
            )

    return results


def strip_comments(source: str) -> str:
    """Remove C comments (/* */ and //) from source text.
    Simpler than tree-sitter for cases where we just need clean text."""
    # Remove block comments.
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    # Remove line comments.
    source = re.sub(r"//[^\n]*", " ", source)
    return source


_NAMED_CAST_RE = re.compile(r"\b(?:static|reinterpret|const|dynamic)_cast\s*<")
_C_POINTER_CAST_RE = re.compile(
    r"\(\s*[A-Za-z_][\w\s:<>,]*?\*+(?:\s+(?:const|volatile|restrict))*\s*\)"
)


def strip_casts(text: str) -> str:
    """Remove C/C++ cast syntax so downstream matchers see the inner expression.

    Handles C++ named casts (``static_cast<T>(x)`` -> ``x``, plus
    ``reinterpret_cast``/``const_cast``/``dynamic_cast``) and C-style pointer
    casts (``(struct foo *)x`` -> ``x``). Only pointer C-casts are stripped, so
    grouping parentheses and value casts like ``(int)`` are left intact.

    This is text-level normalization for the regex-based ownership matchers;
    without it ``return (struct foo *)obj;`` looks like a non-trivial expression
    and ``obj`` is falsely reported as leaked instead of returned.
    """
    return _C_POINTER_CAST_RE.sub("", _remove_named_casts(text))


def _remove_named_casts(text: str) -> str:
    """Replace ``CAST<T>(expr)`` with ``expr`` for the four C++ named casts."""
    while True:
        match = _NAMED_CAST_RE.search(text)
        if not match:
            return text
        # Skip the balanced <...> template argument list.
        index = match.end()
        depth = 1
        while index < len(text) and depth:
            if text[index] == "<":
                depth += 1
            elif text[index] == ">":
                depth -= 1
            index += 1
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text) or text[index] != "(":
            return text  # malformed; stop rather than loop forever
        # Extract the balanced (expr) and splice it in place of the whole cast.
        start = index + 1
        end = start
        depth = 1
        while end < len(text) and depth:
            if text[end] == "(":
                depth += 1
            elif text[end] == ")":
                depth -= 1
            end += 1
        inner = text[start : end - 1]
        text = text[: match.start()] + inner + text[end:]
