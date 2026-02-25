from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Tuple


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_date_str(s: str) -> bool:
    return bool(_DATE_RE.match(s.strip()))


def _quote_single(s: str) -> str:
    # Oracle payload examples use single quotes for date and rosetta table string values.
    s = s.replace("'", "''")
    return f"'{s}'"


def to_rosetta_str(values: Iterable[Any]) -> str:
    # Values are comma-separated, wrapped in single quotes.
    flat: List[str] = []
    for v in values:
        if v is None:
            flat.append("")
        else:
            flat.append(str(v).strip())
    return _quote_single(",".join(flat))


def build_parameter_list(params: Dict[str, Any]) -> str:
    """Build Oracle ERP Integration REST ParameterList string.

    Rules (per OFAM design + FA REST Transactions guidance):
    - Parameter names must be UPPERCASE; unknown/incorrect names are ignored by Fusion.
    - Rosetta tables (_TBL) must be comma-separated lists wrapped in single quotes.
    - Standard date format is YYYY-MM-DD (wrapped in single quotes).
    """
    items: List[str] = []
    for k, v in params.items():
        if v is None:
            continue
        if not isinstance(k, str) or k.upper() != k:
            raise ValueError(f"Parameter name must be UPPERCASE: {k!r}")

        if isinstance(v, (list, tuple)):
            items.append(f"{k}: {to_rosetta_str(v)}")
            continue

        if isinstance(v, str) and (k.endswith("_TBL") or k.endswith("_TABLE")):
            # Already encoded rosetta string, but enforce quoting.
            items.append(f"{k}: {to_rosetta_str([p.strip() for p in v.split(',') if p.strip() != ''])}")
            continue

        if isinstance(v, str) and _is_date_str(v):
            items.append(f"{k}: {_quote_single(v.strip())}")
            continue

        # Keep strings unquoted to match Oracle examples (incl. book codes with spaces).
        items.append(f"{k}: {v}")
    return "{" + ", ".join(items) + "}"


def parse_rosetta(value: Any) -> List[str]:
    """Parse a Rosetta table value coming back from Fusion.

    Fusion returns comma-separated strings, often with empty placeholders like ','.
    """
    if value is None:
        return []
    s = str(value).strip()
    if s == "" or s == ",":
        return []
    parts = [p.strip() for p in s.split(",")]
    # Preserve empty cells? For OFAM transfer builder, we drop empties.
    return [p for p in parts if p != ""]


def ensure_same_len(*tables: List[str]) -> None:
    lens = [len(t) for t in tables]
    if len(set(lens)) != 1:
        raise ValueError(f"Rosetta tables have inconsistent lengths: {lens}")
