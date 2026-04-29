"""Oracle ERP Integrations ParameterList builder for ofam_mass_additions.

Self-contained copy of the helper used by the asset-xfer pipeline so
this package has no import-time dependency on ofam_asset_xfer.

Builds the ``ParameterList`` string consumed by Oracle's
``processTransaction-<handle>`` REST endpoint.

Rules (per OFAM design + FA REST Transactions guidance):
  * Parameter names must be UPPERCASE; Fusion silently ignores misnamed keys.
  * Rosetta tables (``_TBL`` / ``_TABLE`` suffixes) must be comma-separated
    lists wrapped in single quotes.
  * Standard date format is YYYY-MM-DD wrapped in single quotes.
  * Numeric values stay unquoted; everything else is single-quoted.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")


def _is_date_str(s: str) -> bool:
    return bool(_DATE_RE.match(s.strip()))


def _quote_single(s: str) -> str:
    s = s.replace("'", "''")
    return f"'{s}'"


def to_rosetta_str(values: Iterable[Any]) -> str:
    """Encode an iterable as a comma-separated rosetta-table string."""
    flat: List[str] = []
    for v in values:
        if v is None:
            flat.append("")
        else:
            flat.append(str(v).strip())
    return _quote_single(",".join(flat))


def build_parameter_list(params: Dict[str, Any]) -> str:
    """Build the ``ParameterList`` string for a processTransaction call."""
    items: List[str] = []
    for k, v in params.items():
        if v is None:
            continue
        if not isinstance(k, str) or k.upper() != k:
            raise ValueError(f"Parameter name must be UPPERCASE: {k!r}")

        if isinstance(v, (list, tuple)):
            items.append(f"{k}:{to_rosetta_str(v)}")
            continue

        if isinstance(v, str) and (k.endswith("_TBL") or k.endswith("_TABLE")):
            items.append(
                f"{k}:{to_rosetta_str([p.strip() for p in v.split(',') if p.strip() != ''])}"
            )
            continue

        if isinstance(v, str) and _is_date_str(v):
            items.append(f"{k}: {_quote_single(v.strip())}")
            continue

        sv = str(v)
        if _NUMERIC_RE.match(sv.strip()):
            items.append(f"{k}: {sv}")
        else:
            items.append(f"{k}:{_quote_single(sv)}")
    return "{" + ", ".join(items) + "}"
