"""Text-level C call normalization for IoT resource APIs.

Unlike JNI (``(*env)->NewGlobalRef(env, obj)``) or pybind, IoT/firmware SDKs
expose plain C functions: ``pbuf_alloc(...)``, ``socket(...)``, ``close(fd)``,
``pbuf_free(p)``, ``xSemaphoreTake(m, t)``. This module finds those calls and
splits their argument lists so the transfer rules can match acquire/release
APIs by name and pick out the resource argument.
"""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class CCall:
    """One normalized C function call."""

    name: str
    args: tuple[str, ...]
    text: str


# A bare ``identifier(`` call head. We intentionally do not match ``->`` / ``.``
# member calls or ``)(`` call-through-pointer heads; IoT SDK resource APIs are
# free functions, and skipping member calls avoids matching struct method-style
# syntax that the plain-C model cannot reason about.
_CALL_HEAD_RE = re.compile(r"(?:(?<![\w.>])|^)([A-Za-z_]\w*)\s*\(")

# C keywords that take a parenthesized clause but are not function calls.
_NOT_CALLS = frozenset(
    {"if", "for", "while", "switch", "return", "sizeof", "do", "else"}
)


def find_c_calls(text: str) -> list[CCall]:
    """Return normalized C function calls found in ``text``."""
    calls: list[CCall] = []
    seen: set[tuple[int, str]] = set()
    for match in _CALL_HEAD_RE.finditer(text):
        name = match.group(1)
        if name in _NOT_CALLS:
            continue
        args_text, end = _balanced_call_args(text, match.end() - 1)
        if args_text is None:
            continue
        key = (match.start(), name)
        if key in seen:
            continue
        seen.add(key)
        args = tuple(_split_args(args_text))
        calls.append(CCall(name=name, args=args, text=text[match.start() : end]))
    return sorted(calls, key=lambda call: text.find(call.text))


def assigned_variable_for_call(text: str, call: CCall) -> str | None:
    """Return the simple variable assigned from a call, if any."""
    prefix = text.split(call.text, 1)[0]
    if "=" not in prefix:
        return None
    left = prefix.rsplit("=", 1)[0]
    if _has_non_assignment_operator(left):
        return None
    names = re.findall(r"\b[A-Za-z_]\w*\b", left)
    return names[-1] if names else None


def assignment_lhs_for_call(text: str, call: CCall) -> str | None:
    """Return the whole left-hand side expression assigned from a call.

    Lets callers tell a plain local (``int fd``) from an escaping target
    (``st->fd``, ``g_sock``, ``fds[i]``).
    """
    prefix = text.split(call.text, 1)[0]
    if "=" not in prefix:
        return None
    left = prefix.rsplit("=", 1)[0]
    if _has_non_assignment_operator(left):
        return None
    left = left.strip()
    return left or None


def arg_at(call: CCall, index: int) -> str | None:
    """Return argument ``index`` as a plain identifier, unwrapping a leading ``&``.

    IoT release/init APIs frequently take the resource by address
    (``pbuf_free(p)`` but ``pthread_mutex_destroy(&m)``), so a single leading
    address-of is stripped to recover the underlying variable name.
    """
    if index < 0 or index >= len(call.args):
        return None
    return simple_name(call.args[index])


def first_simple_arg(call: CCall) -> str | None:
    return arg_at(call, 0)


def simple_name(text: str) -> str | None:
    """Return ``text`` as a plain identifier, unwrapping a single leading ``&``."""
    text = text.strip()
    if text.startswith("&"):
        text = text[1:].strip()
    return text if re.match(r"^[A-Za-z_]\w*$", text) else None


def _balanced_call_args(text: str, open_paren: int) -> tuple[str | None, int]:
    if open_paren >= len(text) or text[open_paren] != "(":
        return None, open_paren
    depth = 1
    i = open_paren + 1
    start = i
    in_string: str | None = None
    escaped = False
    while i < len(text):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            i += 1
            continue
        if ch in {'"', "'"}:
            in_string = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start:i], i + 1
        i += 1
    return None, i


def _split_args(text: str) -> list[str]:
    args: list[str] = []
    start = 0
    depth = 0
    in_string: str | None = None
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue
        if ch in {'"', "'"}:
            in_string = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(text[start:i].strip())
            start = i + 1
    tail = text[start:].strip()
    if tail:
        args.append(tail)
    return args


def _has_non_assignment_operator(text: str) -> bool:
    return bool(
        re.search(
            r"==|!=|<=|>=|\+=|-=|\*=|/=|%=|&=|\|=|\^=|<<=|>>=",
            text,
        )
    )
