"""Tests for IoT resource lifetime analyzer behavior."""

import sys
import tempfile
import unittest
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

try:
    import tree_sitter  # noqa: F401
    import tree_sitter_c  # noqa: F401
except ImportError:
    HAS_TREE_SITTER = False
else:
    HAS_TREE_SITTER = True

if HAS_TREE_SITTER:
    from analysis.parsing import extract_functions, parse_bytes
    from iot.analyzer import analyze_path
    from iot.resource_transfer import (
        analyze_function_resources,
        set_leak_type_overrides,
    )
    from iot.semantics import load_iot_semantics

from iot.resource_state import Resource, merge_resource_states, ResourceState


class MergeConvergenceTests(unittest.TestCase):
    """The data-flow merge must be deterministic and idempotent so the fixed
    point converges; carrying per-transition provenance once made it oscillate
    across loop back-edges (paho MQTTClient_run never converged)."""

    def _state(self, transition_api):
        return ResourceState(
            (("p", Resource(kind="heap", state="released", line=1, api="malloc",
                            transition_api=transition_api)),)
        )

    def test_merge_is_order_independent(self):
        a, b = self._state("free"), self._state("failed-acquire")
        self.assertEqual(merge_resource_states([a, b]), merge_resource_states([b, a]))

    def test_merge_drops_oscillating_transition_provenance(self):
        merged = merge_resource_states([self._state("free"), self._state(None)])
        p = dict(merged.resources)["p"]
        self.assertIsNone(p.transition_api)
        # Idempotent: re-merging the result with an input yields the same value.
        again = merge_resource_states([merged, self._state("failed-acquire")])
        self.assertEqual(merged, again)


def _result():
    demo = TOOL_ROOT / "tests" / "demo_iot_cases.c"
    return analyze_path(demo)


def _functions_with(result, finding_type):
    return {
        finding["function"]
        for finding in result["findings"]
        if finding["type"] == finding_type
    }


def _analyze_source(source: str):
    source_bytes = source.encode()
    semantics = load_iot_semantics()
    set_leak_type_overrides(semantics)
    functions = extract_functions(parse_bytes(source_bytes), source_bytes)
    return [
        finding
        for function in functions
        for finding in analyze_function_resources(
            function, source_bytes, semantics
        ).findings
    ]


@unittest.skipUnless(HAS_TREE_SITTER, "tree-sitter dependencies are not installed")
class IoTAnalyzerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = _result()

    def test_platforms_loaded(self):
        self.assertIn("posix", self.result["platforms"])
        self.assertIn("lwip", self.result["platforms"])
        self.assertIn("freertos", self.result["platforms"])

    def test_pbuf_leak_on_error_path(self):
        flagged = _functions_with(self.result, "packet_buffer_not_freed")
        self.assertIn("demo_pbuf_leak", flagged)
        self.assertNotIn("demo_pbuf_ok", flagged)
        # Escape / return / global-cache patterns must not be flagged.
        self.assertNotIn("demo_store_field", flagged)
        self.assertNotIn("demo_make_pbuf", flagged)
        self.assertNotIn("demo_cache_global", flagged)

    def test_socket_leak_but_failed_acquire_branch_is_not(self):
        flagged = _functions_with(self.result, "socket_not_closed")
        self.assertIn("demo_socket_leak", flagged)
        self.assertNotIn("demo_socket_ok", flagged)

    def test_malloc_leak_and_ok(self):
        flagged = _functions_with(self.result, "memory_not_freed")
        self.assertIn("demo_malloc_leak", flagged)
        self.assertNotIn("demo_malloc_ok", flagged)

    def test_acquire_in_loop(self):
        flagged = _functions_with(self.result, "acquire_in_loop_without_release")
        self.assertIn("demo_loop_leak", flagged)
        self.assertNotIn("demo_loop_ok", flagged)

    def test_double_release(self):
        flagged = _functions_with(self.result, "double_release")
        self.assertIn("demo_double_free", flagged)

    def test_lock_not_released_on_path(self):
        flagged = _functions_with(self.result, "lock_not_released_on_path")
        self.assertIn("demo_lock_leak", flagged)
        self.assertNotIn("demo_lock_ok", flagged)

    def test_custom_release_wrapper_suppresses_false_leak(self):
        # demo_free_ctx() forwards its arg to free(); cleanup through it must
        # not be reported as a memory leak.
        flagged = _functions_with(self.result, "memory_not_freed")
        self.assertNotIn("demo_wrapper_release_ok", flagged)
        self.assertGreaterEqual(self.result["release_wrappers"], 1)

    def test_reader_taking_resource_is_not_a_release_wrapper(self):
        # demo_read_all(fp) only reads fp; it must not count as closing it, so
        # the real leak in demo_reader_is_not_release is still reported.
        flagged = _functions_with(self.result, "file_not_closed")
        self.assertIn("demo_reader_is_not_release", flagged)

    def test_conditional_release_is_not_an_unconditional_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "conditional.c"
            source.write_text(
                "void maybe_free(void *p, int yes) { if (yes) free(p); }\n"
                "int victim(void) { void *p=malloc(8); "
                "maybe_free(p, 0); return 0; }\n"
            )
            result = analyze_path(source)
        self.assertIn(
            "victim", _functions_with(result, "memory_not_freed")
        )

    def test_cast_and_alias_release_wrapper_is_discovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "cast_alias.c"
            source.write_text(
                "void project_free(char *p) { "
                "void *alias = p; free((void *)alias); }\n"
                "int cleaned(void) { char *p=malloc(8); "
                "project_free(p); return 0; }\n"
            )
            result = analyze_path(source)
        self.assertNotIn(
            "cleaned", _functions_with(result, "memory_not_freed")
        )

    def test_wrapper_fixpoint_is_not_limited_to_four_levels(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "chain.c"
            source.write_text(
                "void w0(void*p){free(p);}"
                "void w1(void*p){w0(p);}"
                "void w2(void*p){w1(p);}"
                "void w3(void*p){w2(p);}"
                "void w4(void*p){w3(p);}"
                "int cleaned(void){void*p=malloc(8);w4(p);return 0;}"
            )
            result = analyze_path(source)
        self.assertNotIn(
            "cleaned", _functions_with(result, "memory_not_freed")
        )

    def test_release_wrapper_is_discovered_across_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "release.c").write_text(
                "void project_free(void *p) { free(p); }\n"
            )
            (root / "user.c").write_text(
                "int cleaned(void) { void *p=malloc(8); "
                "project_free(p); return 0; }\n"
            )
            result = analyze_path(root)
        self.assertNotIn(
            "cleaned", _functions_with(result, "memory_not_freed")
        )

    def test_static_wrapper_does_not_escape_its_source_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "release.c").write_text(
                "static void cleanup(void *p) { free(p); }\n"
            )
            (root / "reader.c").write_text(
                "static void cleanup(void *p) { fread(p,1,1,0); }\n"
                "int victim(void) { void *p=malloc(8); "
                "cleanup(p); return 0; }\n"
            )
            result = analyze_path(root)
        self.assertIn(
            "victim", _functions_with(result, "memory_not_freed")
        )

    def test_leak_reported_once_per_resource(self):
        leaks = [
            finding
            for finding in self.result["findings"]
            if finding["type"] == "packet_buffer_not_freed"
            and finding["function"] == "demo_pbuf_leak"
        ]
        self.assertEqual(len(leaks), 1)

    def test_wrong_release_api_does_not_release_different_resource_kind(self):
        findings = _analyze_source(
            'int f(void) { FILE *fp = fopen("x", "r"); close(fp); return 0; }'
        )
        self.assertEqual(
            [finding.type for finding in findings],
            ["file_not_closed"],
        )

    def test_shared_close_api_matches_fd_resource_kind(self):
        findings = _analyze_source(
            'int f(void) { int fd = open("x", 0); if (fd < 0) return -1; '
            "close(fd); return 0; }"
        )
        self.assertEqual(findings, [])

    def test_trylock_failure_branch_does_not_hold_lock(self):
        findings = _analyze_source(
            "int f(pthread_mutex_t *m) { "
            "if (pthread_mutex_trylock(m) != 0) return -1; "
            "pthread_mutex_unlock(m); return 0; }"
        )
        self.assertEqual(findings, [])

    def test_saved_trylock_status_refines_failure_branch(self):
        findings = _analyze_source(
            "int f(pthread_mutex_t *m) { "
            "int rc = pthread_mutex_trylock(m); "
            "if (rc != 0) return -1; "
            "pthread_mutex_unlock(m); return 0; }"
        )
        self.assertEqual(findings, [])

    def test_out_parameter_acquire_respects_call_success(self):
        findings = _analyze_source(
            "int f(void) { void *task; "
            "if (!xTaskCreate(0, 0, 0, 0, 0, &task)) return -1; "
            "vTaskDelete(task); return 0; }"
        )
        self.assertEqual(findings, [])

    def test_out_parameter_leak_only_on_success_branch(self):
        findings = _analyze_source(
            "int f(void) { void *task; "
            "if (!xTaskCreate(0, 0, 0, 0, 0, &task)) return -1; "
            "return 0; }"
        )
        self.assertEqual(
            [finding.type for finding in findings],
            ["task_not_deleted"],
        )

    def test_saved_out_parameter_status_refines_failure_branch(self):
        findings = _analyze_source(
            "int f(void) { void *task; "
            "int ok = xTaskCreate(0, 0, 0, 0, 0, &task); "
            "if (!ok) return -1; "
            "vTaskDelete(task); return 0; }"
        )
        self.assertEqual(findings, [])

    def test_double_release_preserves_original_acquire_provenance(self):
        findings = _analyze_source(
            "void f(void) {\n"
            "  void *p = malloc(8);\n"
            "  free(p);\n"
            "  free(p);\n"
            "}\n"
        )
        double_release = next(
            finding for finding in findings if finding.type == "double_release"
        )
        self.assertEqual(double_release.acquire_line, 2)
        self.assertEqual(double_release.api_call, "free")

    # --- P10: ownership sinks --------------------------------------------

    _SINK_LIB = (
        "struct node { void *content; };\n"
        "struct list { struct node *head; };\n"
        "void list_add(struct list *L, void *c) {\n"
        "  struct node *n = malloc(8); n->content = c; L->head = n; }\n"
    )

    def test_ownership_sink_suppresses_false_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "sink.c"
            src.write_text(
                self._SINK_LIB
                + "int f(struct list *L) { void *p = malloc(8); "
                "if (!p) return -1; list_add(L, p); return 0; }\n"
            )
            result = analyze_path(src)
        self.assertEqual(_functions_with(result, "memory_not_freed"), set())
        self.assertGreaterEqual(result["ownership_sinks"], 1)

    def test_non_storing_function_is_not_an_ownership_sink(self):
        # use_only() reads its arg but never stores it, so the leak stands.
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "nosink.c"
            src.write_text(
                "int use_only(void *c) { return c != 0; }\n"
                "int f(void) { void *p = malloc(8); use_only(p); return 0; }\n"
            )
            result = analyze_path(src)
        self.assertIn("f", _functions_with(result, "memory_not_freed"))

    def test_conditional_store_is_not_an_unconditional_ownership_sink(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "conditional_sink.c"
            src.write_text(
                "struct ctx { void *p; };\n"
                "void maybe_store(struct ctx *c, void *p, int yes) "
                "{ if (yes) c->p = p; }\n"
                "int victim(struct ctx *c) { void *p = malloc(8); "
                "maybe_store(c, p, 0); return 0; }\n"
            )
            result = analyze_path(src)
        self.assertIn("victim", _functions_with(result, "memory_not_freed"))

    def test_local_array_store_is_not_an_ownership_sink(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "local_store.c"
            src.write_text(
                "void local_only(void *p) { void *tmp[1]; "
                "tmp[0] = p; use(tmp); }\n"
                "int victim(void) { void *p = malloc(8); "
                "local_only(p); return 0; }\n"
            )
            result = analyze_path(src)
        self.assertIn("victim", _functions_with(result, "memory_not_freed"))

    def test_inline_assign_null_check_with_nested_call_refines_failure(self):
        # (p = malloc(sizeof(struct node))) == NULL must read as p == NULL.
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "inline.c"
            src.write_text(
                self._SINK_LIB
                + "int f(struct list *L) { void *p;\n"
                "  if ((p = malloc(sizeof(struct node))) == NULL) { return -1; }\n"
                "  list_add(L, p); return 0; }\n"
            )
            result = analyze_path(src)
        self.assertEqual(_functions_with(result, "memory_not_freed"), set())

    def test_inline_assign_does_not_break_plain_comparison(self):
        # `a == b` must not be rewritten; p genuinely leaks when a == b.
        findings = _analyze_source(
            "int f(int a, int b) { void *p = malloc(8); "
            "if (a == b) return -1; free(p); return 0; }"
        )
        self.assertEqual([f.type for f in findings], ["memory_not_freed"])

    def test_inline_assign_balancing_ignores_parenthesis_in_string(self):
        findings = _analyze_source(
            'int f(void) { char *p; if ((p = strdup(")")) == NULL) '
            "return -1; free(p); return 0; }"
        )
        self.assertEqual(findings, [])

    # --- P13: chained inline assignment ----------------------------------

    def test_chained_inline_assign_tracks_innermost_assignee(self):
        # (ptr = buf = malloc(2)) == NULL must read as buf == NULL, so the
        # failure branch refines buf and the free on the success path clears it.
        findings = _analyze_source(
            "int f(void) { char *ptr, *buf;\n"
            "  if ((ptr = buf = malloc(2)) == NULL) return -1;\n"
            "  free(buf); return 0; }"
        )
        self.assertEqual(findings, [])

    # --- P15: bare truthiness + exit-leak edge refinement ----------------

    def test_bare_truthiness_guarded_free_is_not_a_leak(self):
        # p = pbuf_alloc(); if (p) { ...; pbuf_free(p); } frees on the only
        # path where p is non-NULL; the false branch has p == NULL.
        findings = _analyze_source(
            "void f(void) { struct pbuf *p = pbuf_alloc(0, 0, 0);\n"
            "  if (p) { use(p); pbuf_free(p); } }"
        )
        self.assertEqual(findings, [])

    def test_bare_truthiness_real_leak_still_flagged(self):
        findings = _analyze_source(
            "void f(void) { struct pbuf *p = pbuf_alloc(0, 0, 0);\n"
            "  if (p) { use(p); } }"
        )
        self.assertEqual(
            [f.type for f in findings], ["packet_buffer_not_freed"]
        )

    def test_bare_truthiness_does_not_release_fd_resource(self):
        # 0 is a valid fd, so `if (fd)` must not drop an fd on the false branch.
        findings = _analyze_source(
            'int f(void) { int fd = open("x", 0); if (fd) { work(fd); } return 0; }'
        )
        self.assertEqual([f.type for f in findings], ["fd_not_closed"])

    # --- P16: chained escape to a global/field ---------------------------

    def test_chained_assignment_into_global_is_an_escape(self):
        # fe = malloc(); first = last = fe;  hands fe to a global list.
        findings = _analyze_source(
            "struct E { struct E *next; };\n"
            "static struct E *first, *last;\n"
            "void reg(void) { struct E *fe = malloc(8);\n"
            "  if (first == NULL) { first = last = fe; }\n"
            "  else { last->next = fe; last = fe; } }"
        )
        self.assertEqual(findings, [])

    # --- P17: value-cast alias then return -------------------------------

    def test_resource_returned_through_value_cast_alias(self):
        # ret = (int)fd; return ret;  transfers fd to the caller.
        findings = _analyze_source(
            "int connect_one(void) { int fd = socket(2, 1, 0); int ret = -1;\n"
            "  if (fd < 0) return -1;\n"
            "  ret = (int)fd; return ret; }"
        )
        self.assertEqual(findings, [])

    # --- inferred acquire wrappers --------------------------------------

    def test_acquire_wrapper_is_discovered_across_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "alloc.c").write_text(
                "void *project_alloc(void) { return malloc(8); }\n"
            )
            (root / "user.c").write_text(
                "void victim(void) { void *p = project_alloc(); }\n"
            )
            result = analyze_path(root)
        self.assertGreaterEqual(result["acquire_wrappers"], 1)
        self.assertIn("victim", _functions_with(result, "memory_not_freed"))

    def test_acquire_wrapper_fixpoint_handles_multiple_levels(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "alloc_chain.c"
            source.write_text(
                "void *a0(void) { return malloc(8); }\n"
                "void *a1(void) { return a0(); }\n"
                "void *a2(void) { return a1(); }\n"
                "void victim(void) { void *p = a2(); }\n"
            )
            result = analyze_path(source)
        self.assertGreaterEqual(result["acquire_wrappers"], 3)
        self.assertIn("victim", _functions_with(result, "memory_not_freed"))

    # --- use-after-release (use after free/close) ------------------------

    def test_use_after_free_passed_to_call(self):
        findings = _analyze_source(
            "void f(void) { void *p = malloc(8); free(p); use(p); }"
        )
        self.assertEqual([f.type for f in findings], ["use_after_release"])

    def test_use_after_close_passed_to_call(self):
        findings = _analyze_source(
            'int f(void) { int fd = open("x", 0); if (fd < 0) return -1;\n'
            "  close(fd); read(fd, 0, 0); return 0; }"
        )
        self.assertEqual([f.type for f in findings], ["use_after_release"])

    def test_use_after_free_dereference(self):
        findings = _analyze_source(
            "struct S { int x; };\n"
            "void f(void) { struct S *p = malloc(8); free(p); p->x = 1; }"
        )
        self.assertEqual([f.type for f in findings], ["use_after_release"])

    def test_use_after_release_through_alias(self):
        findings = _analyze_source(
            "void f(void) { void *p = malloc(8); void *q = p; "
            "free(p); use(q); }"
        )
        self.assertEqual([f.type for f in findings], ["use_after_release"])

    def test_use_after_release_unary_dereference(self):
        findings = _analyze_source(
            "void f(void) { int *p = malloc(8); free(p); int x = *p; }"
        )
        self.assertEqual([f.type for f in findings], ["use_after_release"])

    def test_returning_released_resource_is_flagged(self):
        findings = _analyze_source(
            "void *f(void) { void *p = malloc(8); free(p); return p; }"
        )
        self.assertEqual([f.type for f in findings], ["use_after_release"])

    def test_returning_failed_acquire_value_is_not_use_after_release(self):
        findings = _analyze_source(
            "void *f(void) { void *p = malloc(8); "
            "if (!p) return p; free(p); return 0; }"
        )
        self.assertNotIn("use_after_release", [f.type for f in findings])

    def test_reassigned_after_free_is_not_use_after_release(self):
        findings = _analyze_source(
            "void f(void) { void *p = malloc(8); free(p);\n"
            "  p = malloc(16); free(p); }"
        )
        self.assertEqual(findings, [])

    def test_double_free_is_not_also_reported_as_use_after_release(self):
        findings = _analyze_source(
            "void f(void) { void *p = malloc(8); free(p); free(p); }"
        )
        self.assertEqual([f.type for f in findings], ["double_release"])

    def test_borrowed_pointer_truthiness_is_not_use_after_release(self):
        # peek() is not an acquire, so q is a never-acquired (declared) local;
        # `if (q)` must not fabricate a released state, so use(q) is not UAF.
        findings = _analyze_source(
            "void f(void) { void *q = peek(); if (q) { use(q); } }"
        )
        self.assertEqual(findings, [])

    # --- owned overwrite (lost handle) -----------------------------------

    def test_owned_overwrite_leaks_first_acquisition(self):
        findings = _analyze_source(
            "void f(void) {\n"
            "  void *p = malloc(8);\n"
            "  p = malloc(16);\n"
            "  free(p);\n}"
        )
        self.assertEqual([f.type for f in findings], ["owned_overwrite"])

    def test_overwrite_after_release_is_not_flagged(self):
        findings = _analyze_source(
            "void f(void) {\n"
            "  void *p = malloc(8);\n"
            "  free(p);\n"
            "  p = malloc(16);\n"
            "  free(p);\n}"
        )
        self.assertEqual(findings, [])

    def test_overwrite_after_escape_is_not_flagged(self):
        findings = _analyze_source(
            "struct C { void *b; };\n"
            "void f(struct C *c) {\n"
            "  void *p = malloc(8);\n"
            "  c->b = p;\n"
            "  p = malloc(16);\n"
            "  free(p);\n}"
        )
        self.assertEqual(findings, [])

    def test_owned_overwrite_by_borrowed_value(self):
        findings = _analyze_source(
            "void f(void *borrowed) {\n"
            "  void *p = malloc(8);\n"
            "  p = borrowed;\n"
            "}"
        )
        self.assertEqual([f.type for f in findings], ["owned_overwrite"])

    def test_out_parameter_acquire_overwrites_owned_handle(self):
        findings = _analyze_source(
            "void f(void) {\n"
            "  void *task;\n"
            "  xTaskCreate(0,0,0,0,0,&task);\n"
            "  xTaskCreate(0,0,0,0,0,&task);\n"
            "}"
        )
        self.assertIn("owned_overwrite", [f.type for f in findings])

    def test_realloc_self_update_is_not_owned_overwrite(self):
        findings = _analyze_source(
            "void f(void) {\n"
            "  void *p = malloc(8);\n"
            "  p = realloc(p, 16);\n"
            "  free(p);\n"
            "}"
        )
        self.assertNotIn("owned_overwrite", [f.type for f in findings])

    def test_assigning_null_is_not_owned_overwrite(self):
        # `p = NULL;` is defensive clearing (almost always right after a free,
        # often through a custom free wrapper the inference did not catch). It
        # must not be flagged as a lost-handle overwrite -- this was a large
        # false-positive source on real SDKs (e.g. `free_wrapper(x); x = NULL;`).
        findings = _analyze_source(
            "void f(void) {\n"
            "  void *p = malloc(8);\n"
            "  some_free(p);\n"
            "  p = NULL;\n}"
        )
        self.assertNotIn("owned_overwrite", {f.type for f in findings})

    def test_loop_reacquire_is_not_owned_overwrite(self):
        # A loop re-acquiring into the same variable is acquire_in_loop's job;
        # it must not also be reported as a sequential owned_overwrite.
        result = self._loop_findings(
            "void f(int n) { int i; void *p = 0;\n"
            "  for (i = 0; i < n; i++) { p = malloc(8); use(p); } }"
        )
        self.assertEqual(
            _functions_with(result, "owned_overwrite"), set()
        )

    # --- P2: protocol-order (typestate) ----------------------------------

    _PROTO_SPEC = str(TOOL_ROOT / "tests" / "demo_protocol_spec.json")

    def test_protocol_order_findings(self):
        demo = TOOL_ROOT / "tests" / "demo_protocol_cases.c"
        result = analyze_path(demo, api_specs=[self._PROTO_SPEC])
        flagged = _functions_with(result, "invalid_protocol_transition")
        self.assertIn("demo_proto_publish_before_connect", flagged)
        self.assertIn("demo_proto_use_after_destroy", flagged)
        # Legal lifecycles and untracked clients must not be flagged.
        self.assertNotIn("demo_proto_ok", flagged)
        self.assertNotIn("demo_proto_reconnect_ok", flagged)
        self.assertNotIn("demo_proto_unknown_client", flagged)

    def test_no_protocols_bundled_by_default(self):
        # The engine is generic: no library-specific protocol ships by default,
        # so a default scan never emits protocol findings.
        semantics = load_iot_semantics()
        self.assertEqual(semantics.protocols, ())

    def test_protocol_finding_reports_state_and_api(self):
        demo = TOOL_ROOT / "tests" / "demo_protocol_cases.c"
        result = analyze_path(demo, api_specs=[self._PROTO_SPEC])
        uad = next(
            f
            for f in result["findings"]
            if f["type"] == "invalid_protocol_transition"
            and f["function"] == "demo_proto_use_after_destroy"
        )
        self.assertEqual(uad["state"], "destroyed")
        self.assertEqual(uad["variable"], "c")

    # --- P11: loop check escape filtering --------------------------------

    def _loop_findings(self, body: str):
        # The loop check is an analyzer producer, so it only runs through
        # analyze_path (not analyze_function_resources directly).
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "loop.c"
            src.write_text(body)
            return analyze_path(src)

    def test_loop_local_array_store_remains_a_leak(self):
        # A local array does not outlive the function. Keep the finding and
        # attribute it to pcb[i], never to the index variable i.
        result = self._loop_findings(
            "void f(void) { void *pcb[8]; int i;\n"
            "  for (i = 0; i < 8; i++) { pcb[i] = tcp_new(); } }"
        )
        findings = [
            finding
            for finding in result["findings"]
            if finding["type"] == "acquire_in_loop_without_release"
        ]
        self.assertEqual(
            [(finding["function"], finding["variable"]) for finding in findings],
            [("f", "pcb[i]")],
        )

    def test_loop_global_array_store_escapes(self):
        result = self._loop_findings(
            "void *pcb[8]; void f(void) { int i;\n"
            "  for (i = 0; i < 8; i++) { pcb[i] = tcp_new(); } }"
        )
        self.assertEqual(
            _functions_with(result, "acquire_in_loop_without_release"), set()
        )

    def test_loop_local_acquire_without_release_still_flagged(self):
        result = self._loop_findings(
            "void f(int n) { int i;\n"
            "  for (i = 0; i < n; i++) { void *p = malloc(8); use(p); } }"
        )
        self.assertIn(
            "f", _functions_with(result, "acquire_in_loop_without_release")
        )

    # --- P12: test/doc exclusion -----------------------------------------

    def test_test_directory_excluded_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "test").mkdir()
            (root / "src" / "real.c").write_text(
                "int r(void) { void *p = malloc(8); return 0; }\n"
            )
            (root / "test" / "t.c").write_text(
                "int t(void) { void *p = malloc(8); return 0; }\n"
            )
            default = analyze_path(root)
            with_tests = analyze_path(root, include_tests=True)
        self.assertIn("r", _functions_with(default, "memory_not_freed"))
        self.assertNotIn("t", _functions_with(default, "memory_not_freed"))
        self.assertGreaterEqual(default["excluded_test_files"], 1)
        self.assertIn("t", _functions_with(with_tests, "memory_not_freed"))

    def test_max_files_counts_after_test_directory_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "test").mkdir()
            (root / "zsrc").mkdir()
            (root / "test" / "a.c").write_text("int t(void) { return 0; }\n")
            (root / "zsrc" / "z.c").write_text(
                "int real(void) { void *p = malloc(8); return 0; }\n"
            )
            result = analyze_path(root, max_files=1)
        self.assertEqual(result["files_analyzed"], 1)
        self.assertIn("real", _functions_with(result, "memory_not_freed"))


if __name__ == "__main__":
    unittest.main()
