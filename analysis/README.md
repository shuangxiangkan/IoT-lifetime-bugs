# analysis

Generic, check-agnostic C/C++ code analysis.

This folder should stay independent of any check-specific API rules (JNI
lifetime, CPython refcounts, etc.). It provides:

- `parsing.py`: Tree-sitter parsing for C (with optional C++ support) and
  function, call, assignment, return, global declaration, and struct extraction.
- `controlflow.py`: statement-level control-flow graph builder.
- `dataflow.py`: forward data-flow solver over a control-flow graph.
- `sources.py`: project root and C/C++ source-file discovery.

Use this layer when an analyzer needs structured C/C++ information but should not know about any particular ownership/lifetime API rules.
