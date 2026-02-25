from __future__ import annotations

import re
from typing import Any, Dict


_VAR_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_\.\-]+)\s*\}\}")


def _get_from_context(context: Dict[str, Any], path: str) -> Any:
    cur: Any = context
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def render(value: Any, context: Dict[str, Any]) -> Any:
    """Render a template value using {{var.path}} substitutions.

    This is used for native xBook handle parameter templates, where exact parameter
    names differ by tenant/version but OFAM still wants orchestration and pre-read.
    """
    if isinstance(value, dict):
        return {k: render(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, context) for v in value]
    if not isinstance(value, str):
        return value

    def repl(match: re.Match) -> str:
        key = match.group(1)
        v = _get_from_context(context, key)
        return "" if v is None else str(v)

    return _VAR_RE.sub(repl, value)
