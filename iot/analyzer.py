#!/usr/bin/env python3
"""IoT resource lifetime checks built on generic C/C++ extraction.

This analyzer is a static pre-screening layer. It does not compile or execute
the target project; it emits JSON candidate findings for human or LLM review.
"""

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import sys

from analysis.parsing import extract_functions, parse_bytes_for_file, walk_descendants
from analysis.sources import (
    discover_source_files,
    find_project_root,
    first_unscannable_cpp_file,
)
from iot.calls import (
    assigned_variable_for_call,
    assignment_lhs_for_call,
    find_c_calls,
)
from iot.resource_transfer import (
    analyze_function_resources,
    set_leak_type_overrides,
)
from iot.protocol import analyze_function_protocol
from iot.semantics import IoTSemantics, load_iot_semantics
from iot.wrappers import (
    discover_acquire_wrappers,
    discover_ownership_sinks,
    discover_release_wrappers,
)


def analyze_file(
    filepath: Path,
    *,
    project_root: Path | None = None,
    semantics: IoTSemantics | None = None,
    discover_wrappers: bool = True,
) -> dict:
    """Analyze one C/C++ file for IoT resource lifetime issues.

    When ``semantics`` is omitted, a standalone run loads the bundled specs and
    discovers custom release wrappers from this file's own functions. When
    ``analyze_path`` drives a whole project it passes an already-augmented
    ``semantics`` with ``discover_wrappers=False`` so wrappers defined in other
    files are also recognized.
    """
    if semantics is None:
        semantics = load_iot_semantics()
        set_leak_type_overrides(semantics)
    project_root = project_root or filepath.parent
    source_bytes = filepath.read_bytes()
    tree = parse_bytes_for_file(source_bytes, filepath)
    functions = extract_functions(tree, source_bytes)

    try:
        rel = str(filepath.relative_to(project_root))
    except ValueError:
        rel = str(filepath)

    for func in functions:
        func["_source_file"] = rel
        func["_source_bytes"] = source_bytes

    if discover_wrappers:
        acquire_wrappers = discover_acquire_wrappers(functions, semantics)
        wrappers = discover_release_wrappers(functions, semantics)
        sinks = discover_ownership_sinks(functions, semantics)
        if wrappers or sinks or acquire_wrappers:
            semantics = semantics.augmented(
                wrappers=wrappers,
                sinks=sinks,
                acquire_wrappers=acquire_wrappers,
                source_file=rel,
            )

    findings, warnings = _collect_findings(functions, source_bytes, rel, semantics)
    return {
        "file": rel,
        "functions_analyzed": len(functions),
        "findings": findings,
        "warnings": warnings,
    }


def _collect_findings(
    functions: list[dict], source_bytes: bytes, rel: str, semantics: IoTSemantics
) -> tuple[list[dict], list[str]]:
    findings: list[dict] = []
    warnings: list[str] = []
    seen_findings = set()
    for func in functions:
        for produce in _FINDING_PRODUCERS:
            try:
                produced = produce(func, source_bytes, semantics)
            except Exception as exc:
                warnings.append(
                    f"{rel}: {produce.__name__} failed on function "
                    f"{func.get('name', '?')!r}: {type(exc).__name__}: {exc}"
                )
                continue
            for finding in produced:
                finding["file"] = rel
                key = _finding_key(finding)
                if key not in seen_findings:
                    seen_findings.add(key)
                    findings.append(finding)
    return findings, warnings


_TEST_DOC_DIRS = frozenset(
    {
        "test",
        "tests",
        "unittest",
        "unittests",
        "doc",
        "docs",
        "example",
        "examples",
        "sample",
        "samples",
        "demo",
        "demos",
    }
)


def _is_test_or_doc(rel: str) -> bool:
    """True when a project-relative path lives under a test/doc/example dir."""
    return any(part.lower() in _TEST_DOC_DIRS for part in Path(rel).parts)


def analyze_path(
    target: str | Path,
    *,
    max_files: int = 0,
    api_specs: list[str | Path] | str | Path | None = None,
    include_tests: bool = False,
) -> dict:
    """Analyze a file or directory for IoT resource lifetime issues.

    Test, doc, and example directories are skipped by default: on real IoT
    repos their findings dominate and are noise (tests intentionally leak or
    free in a separate teardown). Pass ``include_tests=True`` to scan them.
    """
    target_path = Path(target).resolve()
    project_root = find_project_root(target_path)
    scan_root = target_path if target_path.is_dir() else target_path.parent
    semantics = load_iot_semantics(api_specs)
    set_leak_type_overrides(semantics)

    findings = []
    skipped = []
    warnings = []
    functions_analyzed = 0
    files_analyzed = 0
    excluded_test_files = 0

    unscannable_cpp = first_unscannable_cpp_file(target_path)
    if unscannable_cpp is not None:
        message = (
            "tree-sitter-cpp is not installed; C++ sources such as "
            f"{unscannable_cpp} were skipped. Install it with "
            "'pip install tree-sitter-cpp' to scan C++ files."
        )
        warnings.append(message)
        print(message, file=sys.stderr)

    # Pass 1: parse every file once, caching its functions, and collect all
    # functions so custom release wrappers (e.g. a deallocator defined in
    # util.c but used in main.c) are discovered project-wide before analysis.
    parsed: list[tuple[str, bytes, list[dict]]] = []
    all_functions: list[dict] = []
    selected_files = 0
    for filepath in discover_source_files(target_path):
        try:
            rel = str(filepath.relative_to(project_root))
        except ValueError:
            rel = str(filepath)
        # Test/doc exclusion is judged relative to the scan root, so pointing
        # the tool directly at a file (or at a test dir) still scans it; only
        # test/doc subtrees *inside* a scanned project are skipped.
        try:
            scan_rel = str(filepath.relative_to(scan_root))
        except ValueError:
            scan_rel = rel
        if not include_tests and _is_test_or_doc(scan_rel):
            excluded_test_files += 1
            continue
        if max_files and selected_files >= max_files:
            break
        try:
            source_bytes = filepath.read_bytes()
        except OSError as exc:
            skipped.append({"file": str(filepath), "reason": str(exc)})
            continue
        tree = parse_bytes_for_file(source_bytes, filepath)
        functions = extract_functions(tree, source_bytes)
        for func in functions:
            func["_source_file"] = rel
            func["_source_bytes"] = source_bytes
        parsed.append((rel, source_bytes, functions))
        all_functions.extend(functions)
        selected_files += 1

    acquire_wrappers = discover_acquire_wrappers(all_functions, semantics)
    wrappers = discover_release_wrappers(all_functions, semantics)
    sinks = discover_ownership_sinks(all_functions, semantics)

    # Pass 2: run the producers with the wrapper/sink/acquire-augmented semantics.
    for rel, source_bytes, functions in parsed:
        if not functions:
            continue
        file_semantics = semantics.augmented(
            wrappers=wrappers,
            sinks=sinks,
            acquire_wrappers=acquire_wrappers,
            source_file=rel,
        )
        file_findings, file_warnings = _collect_findings(
            functions, source_bytes, rel, file_semantics
        )
        for message in file_warnings:
            warnings.append(message)
            print(message, file=sys.stderr)
        files_analyzed += 1
        functions_analyzed += len(functions)
        findings.extend(file_findings)

    by_type = defaultdict(int)
    by_confidence = defaultdict(int)
    for finding in findings:
        by_type[finding["type"]] += 1
        by_confidence[finding["confidence"]] += 1

    return {
        "project_root": str(project_root),
        "scan_root": str(scan_root),
        "platforms": semantics.platforms(),
        "release_wrappers": len(
            {
                (next(iter(spec.release_apis)), spec.scope_file)
                for spec in wrappers
            }
        ),
        "release_wrapper_specs": len(wrappers),
        "ownership_sinks": len({(s.name, s.scope_file) for s in sinks}),
        "ownership_sink_specs": len(sinks),
        "acquire_wrappers": len(
            {(next(iter(s.acquire_apis)), s.scope_file) for s in acquire_wrappers}
        ),
        "excluded_test_files": excluded_test_files,
        "files_analyzed": files_analyzed,
        "functions_analyzed": functions_analyzed,
        "findings": findings,
        "summary": {
            "total_findings": len(findings),
            "by_type": dict(by_type),
            "by_confidence": dict(by_confidence),
        },
        "skipped_files": skipped,
        "warnings": warnings,
    }


_LOOP_TYPES = frozenset(
    {"for_statement", "while_statement", "do_statement", "for_range_loop"}
)


def check_acquire_in_loop_without_release(
    func, source_bytes: bytes, semantics: IoTSemantics
):
    """Report resources acquired inside a loop with no release in the loop body.

    A small leak per iteration is the canonical IoT failure: a reconnect or
    poll loop that runs for the device's whole lifetime exhausts a pool, fd
    table, or task list one acquisition at a time.
    """
    findings = []
    params = _parameter_names(func.get("parameters", ""))
    local_names = _function_local_names(func, source_bytes)
    loops = [
        node for node in walk_descendants(func["body_node"]) if node.type in _LOOP_TYPES
    ]
    for loop in loops:
        # Scan only this loop's own body (nested loop bodies removed) so an
        # acquire in an inner loop is attributed to that inner loop alone.
        loop_text = _loop_own_text(loop, source_bytes, loops)
        calls = find_c_calls(loop_text)
        released = {
            arg
            for call in calls
            if semantics.release_specs(call.name)
            or semantics.lock_release_specs(call.name)
            for arg in _release_args(call, semantics)
        }
        for call in calls:
            spec = semantics.acquire_spec(call.name)
            if spec is None or spec.acquire_result != "return":
                continue
            statement_text = _statement_containing_call(loop_text, call.text)
            # Distinguish longer-lived stores from local aggregates. A global
            # or parameter-backed ``pcb[i]`` escapes, while a local ``pcb[i]``
            # remains this function's responsibility and must still be
            # reported (without mis-attributing the resource to ``i``).
            lhs = assignment_lhs_for_call(statement_text, call)
            if lhs is not None:
                store_scope = _store_scope(lhs, params, local_names)
                if store_scope == "escape":
                    continue
                if store_scope == "local_aggregate":
                    variable = lhs.strip()
                else:
                    variable = assigned_variable_for_call(statement_text, call)
            else:
                variable = assigned_variable_for_call(statement_text, call)
            if not variable or variable in released:
                continue
            findings.append(
                {
                    "type": "acquire_in_loop_without_release",
                    "function": func["name"],
                    "line": loop.start_point[0] + 1,
                    "confidence": "medium",
                    "detail": (
                        f"IoT resource '{variable}' ({spec.kind}) from "
                        f"{call.name}() is acquired inside a loop without a "
                        "matching release in the loop body"
                    ),
                    "variable": variable,
                    "api_call": call.name,
                }
            )
    return findings


def _statement_containing_call(text: str, call_text: str) -> str:
    """Narrow loop text to the statement containing this call.

    The generic assignment helper expects one statement; feeding it an entire
    ``for`` body lets earlier ``i = 0`` assignments contaminate the lhs.
    """
    prefix, separator, suffix = text.partition(call_text)
    if not separator:
        return text
    boundary = max(prefix.rfind(";"), prefix.rfind("{"), prefix.rfind("}"))
    return prefix[boundary + 1 :] + call_text + suffix.split(";", 1)[0]


def _store_scope(
    lhs: str, params: set[str], local_names: set[str]
) -> str:
    """Classify an acquisition lvalue as local, local aggregate, or escape."""
    lhs = lhs.strip()
    structural = (
        "->" in lhs
        or "." in lhs
        or "[" in lhs
        or bool(re.match(r"^\s*\(?\s*\*+\s*[A-Za-z_]\w*\s*\)?$", lhs))
    )
    names = re.findall(r"\b[A-Za-z_]\w*\b", lhs)
    if not names:
        return "local"
    base = names[0] if structural else names[-1]
    if base in params or base not in local_names:
        return "escape"
    return "local_aggregate" if structural else "local"


def _function_local_names(func: dict, source_bytes: bytes) -> set[str]:
    names = set()
    for node in walk_descendants(func["body_node"], "declaration"):
        text = source_bytes[node.start_byte : node.end_byte].decode(
            "utf-8", errors="ignore"
        )
        left = text.strip().rstrip(";").split("=", 1)[0]
        identifiers = re.findall(r"\b[A-Za-z_]\w*\b", left)
        if identifiers:
            names.add(identifiers[-1])
    return names


def _parameter_names(text: str) -> set[str]:
    text = text.strip()
    if not text or text == "void":
        return set()
    names = set()
    for piece in text.split(","):
        identifiers = re.findall(r"\b[A-Za-z_]\w*\b", piece)
        if identifiers:
            names.add(identifiers[-1])
    return names


def _release_args(call, semantics: IoTSemantics):
    from iot.calls import arg_at

    specs = (
        semantics.release_specs(call.name)
        or semantics.lock_release_specs(call.name)
    )
    return [
        var
        for spec in specs
        if (var := arg_at(call, spec.release_arg)) is not None
    ]


def _loop_own_text(loop, source_bytes: bytes, all_loops: list) -> str:
    spans = sorted(
        (other.start_byte, other.end_byte)
        for other in all_loops
        if other is not loop
        and other.start_byte >= loop.start_byte
        and other.end_byte <= loop.end_byte
    )
    maximal: list[tuple[int, int]] = []
    for start, end in spans:
        if maximal and start < maximal[-1][1]:
            continue
        maximal.append((start, end))

    out = bytearray()
    cursor = loop.start_byte
    for start, end in maximal:
        out += source_bytes[cursor:start]
        cursor = end
    out += source_bytes[cursor : loop.end_byte]
    return out.decode("utf-8", errors="ignore")


def _dataflow_findings(func, source_bytes: bytes, semantics: IoTSemantics) -> list[dict]:
    analysis = analyze_function_resources(func, source_bytes, semantics)
    return [
        {
            "type": finding.type,
            "function": finding.function,
            "line": finding.line,
            "confidence": finding.confidence,
            "detail": finding.detail,
            "variable": finding.variable,
            "acquire_line": finding.acquire_line,
            "api_call": finding.api_call,
        }
        for finding in analysis.findings
    ]


def _protocol_findings(func, source_bytes: bytes, semantics: IoTSemantics) -> list[dict]:
    return [
        {
            "type": finding.type,
            "function": finding.function,
            "line": finding.line,
            "confidence": finding.confidence,
            "detail": finding.detail,
            "variable": finding.variable,
            "state": finding.state,
            "api_call": finding.api_call,
        }
        for finding in analyze_function_protocol(func, source_bytes, semantics)
    ]


# Per-function producers. analyze_file runs each with isolated error handling so
# one failing analysis neither loses the others nor fails silently.
_FINDING_PRODUCERS = (
    _dataflow_findings,
    check_acquire_in_loop_without_release,
    _protocol_findings,
)


def _finding_key(finding: dict) -> tuple:
    return (
        finding.get("type"),
        finding.get("file"),
        finding.get("function"),
        finding.get("line"),
        finding.get("variable"),
        finding.get("api_call"),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", nargs="?", default=".")
    parser.add_argument("--max-files", type=int, default=0)
    parser.add_argument(
        "--api-specs",
        action="append",
        help="JSON spec file(s) to use instead of the bundled api_specs/",
    )
    parser.add_argument(
        "--include-tests",
        action="store_true",
        help="Also scan test/doc/example directories (skipped by default)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = analyze_path(
            args.target,
            max_files=args.max_files,
            api_specs=args.api_specs,
            include_tests=args.include_tests,
        )
    except Exception as exc:
        json.dump({"error": str(exc), "type": type(exc).__name__}, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 1
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
