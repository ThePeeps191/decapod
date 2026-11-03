"""Text helpers: truncation, token estimates and robust JSON extraction."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)


def estimate_tokens(text: str) -> int:
    """Cheap, provider-agnostic token estimate (~4 characters per token)."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def truncate(text: str, max_chars: int, marker: str = "\n…[truncated {n} chars]") -> str:
    """Truncate ``text`` to at most ``max_chars`` characters, noting how much was cut."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    cut = len(text) - max_chars
    suffix = marker.format(n=cut)
    keep = max(0, max_chars - len(suffix))
    return text[:keep] + suffix


def one_line(text: str, max_chars: int = 160) -> str:
    """Collapse whitespace into a single line and truncate with an ellipsis."""
    flat = " ".join(str(text).split())
    if len(flat) <= max_chars:
        return flat
    return flat[: max(0, max_chars - 1)] + "…"


def string_leaves(value: Any) -> list[str]:
    """Every string (keys included) inside nested dicts/lists, unescaped."""
    out: list[str] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            for key, val in item.items():
                out.append(str(key))
                stack.append(val)
        elif isinstance(item, list | tuple | set):
            stack.extend(item)
        elif item is not None:
            out.append(str(item))
    return out


def json_dumps(value: Any, *, indent: int | None = None) -> str:
    """JSON-encode with sane defaults for Decapod payloads (datetimes, sets, models)."""
    return json.dumps(value, default=_json_default, ensure_ascii=False, indent=indent)


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, set | frozenset | tuple):
        return list(obj)
    if hasattr(obj, "total_seconds"):
        return obj.total_seconds()
    return str(obj)


def _scan_balanced(text: str, start: int) -> int | None:
    """Return the index just past the JSON value starting at ``start`` or None."""
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return i + 1 if ch == closer else None
    return None


def extract_json(text: str) -> Any:
    """Extract the first JSON object/array from model output.

    Handles bare JSON, fenced ```json blocks and JSON embedded in prose.
    Raises ``ValueError`` if nothing parseable is found.
    """
    if text is None:
        raise ValueError("no text to parse")
    stripped = text.strip()
    candidates: list[str] = []
    if stripped:
        candidates.append(stripped)
    candidates.extend(m.group(1).strip() for m in _FENCE_RE.finditer(text))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
    for i, ch in enumerate(text):
        if ch in "{[":
            end = _scan_balanced(text, i)
            if end is None:
                continue
            try:
                return json.loads(text[i:end])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON value found in model output")
