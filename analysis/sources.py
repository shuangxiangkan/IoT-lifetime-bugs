#!/usr/bin/env python3
"""Locate the C/C++ source files (and project root) to analyze."""

from collections.abc import Generator
from pathlib import Path

from .parsing import (
    ALL_SOURCE_EXTENSIONS,
    C_EXTENSIONS,
    CPP_EXTENSIONS,
    is_cpp_available,
)


EXCLUDE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".tox",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        ".eggs",
        "egg-info",
    }
)

PROJECT_MARKERS = (
    ".git",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "CMakeLists.txt",
    "meson.build",
    "Makefile",
)

def find_project_root(start: Path) -> Path:
    """Find a likely project root by walking upward to common markers."""
    current = start if start.is_dir() else start.parent
    for _ in range(20):
        if any((current / marker).exists() for marker in PROJECT_MARKERS):
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return start if start.is_dir() else start.parent


def source_extensions(*, include_cpp: bool | None = None) -> frozenset[str]:
    """Return source extensions to scan."""
    if include_cpp is None:
        include_cpp = is_cpp_available()
    return ALL_SOURCE_EXTENSIONS if include_cpp else C_EXTENSIONS


def first_unscannable_cpp_file(
    root: Path,
    *,
    exclude_dirs: frozenset[str] = EXCLUDE_DIRS,
) -> Path | None:
    """Return the first C++ source that discovery will skip, or None.

    When tree-sitter-cpp is not installed, ``discover_source_files`` omits
    ``.cpp``/``.cc``/``.cxx``/``.hpp`` files (parsing them with the C grammar
    would produce garbage). This helper lets callers surface a warning instead
    of silently skipping C++ sources. It returns at the first match without
    materializing or sorting the whole tree, so it is cheap even on large
    trees. Returns None when the C++ parser is available.
    """
    if is_cpp_available():
        return None
    if root.is_file():
        return root if root.suffix in CPP_EXTENSIONS else None
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in CPP_EXTENSIONS:
            continue
        try:
            parts = set(path.relative_to(root).parts)
        except ValueError:
            continue
        if parts & exclude_dirs:
            continue
        return path
    return None


def discover_source_files(
    root: Path,
    *,
    max_files: int = 0,
    include_cpp: bool | None = None,
    exclude_dirs: frozenset[str] = EXCLUDE_DIRS,
) -> Generator[Path, None, None]:
    """Discover C/C++ source files under root, excluding common build dirs."""
    exts = source_extensions(include_cpp=include_cpp)
    count = 0
    if root.is_file():
        if root.suffix in exts:
            yield root
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in exts:
            continue
        try:
            parts = set(path.relative_to(root).parts)
        except ValueError:
            continue
        if parts & exclude_dirs:
            continue
        yield path
        count += 1
        if max_files and count >= max_files:
            return
