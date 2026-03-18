# =============================================================================
# MERGED FILE — 9 original modules joined as-is with section markers.
#
#   Section 1 → template.py
#   Section 2 → store.py
#   Section 3 → merge_ndjson.py
#   Section 4 → bip_client.py
#   Section 5 → ccid_resolver.py
#   Section 6 → oracle_client.py
#   Section 7 → gcs_publisher.py
#   Section 8 → fusion_sync.py
#   Section 9 → job.py
#
# To find section boundaries:
#   grep -n "^# >>> FILE\|^# <<< END" bundle.py
# =============================================================================

# >>> FILE: template.py >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
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

    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        v = _get_from_context(context, key)
        return "" if v is None else str(v)

    return _VAR_RE.sub(repl, value)
# <<< END: template.py <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

# >>> FILE: store.py >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
from __future__ import annotations

import json
import os
import posixpath
from dataclasses import dataclass
from typing import Any, Tuple

import fsspec  # type: ignore[import-untyped]


def _join_uri(base_uri: str, *parts: str) -> str:
    # Safe join for both local paths and fsspec URIs.
    if "://" in base_uri:
        path = base_uri
        for p in parts:
            path = posixpath.join(path.rstrip("/"), p.lstrip("/"))
        return path
    return os.path.join(base_uri, *parts)


@dataclass(frozen=True)
class ArtifactStore:
    base_uri: str

    def _fs_and_path(self, uri: str) -> Tuple[Any, str]:
        fs, path = fsspec.core.url_to_fs(uri)
        return fs, path

    def ensure_dir(self, rel_dir: str = "") -> None:
        """Ensure the directory exists."""
        uri = _join_uri(self.base_uri, rel_dir) if rel_dir else self.base_uri
        fs, path = self._fs_and_path(uri)
        # Many object stores don't have real directories; makedirs is best-effort.
        try:
            fs.makedirs(path, exist_ok=True)
        except Exception:
            # Ignore if backend doesn't support directories.
            pass

    def write_text(self, rel_path: str, text: str, encoding: str = "utf-8") -> str:
        """Write text content to a relative path."""
        uri = _join_uri(self.base_uri, rel_path)
        fs, path = self._fs_and_path(uri)
        parent = os.path.dirname(path)
        try:
            fs.makedirs(parent, exist_ok=True)
        except Exception:
            pass
        with fs.open(path, "w", encoding=encoding) as f:
            f.write(text)
        return uri

    def write_json(self, rel_path: str, obj: Any, indent: int = 2) -> str:
        """Serialise obj as JSON and write it."""
        return self.write_text(
            rel_path,
            json.dumps(obj, indent=indent, sort_keys=True),
        )

    def read_text(self, uri_or_rel: str, encoding: str = "utf-8") -> str:
        """Read and return text from a URI or relative path."""
        uri = (
            uri_or_rel
            if "://" in uri_or_rel or os.path.isabs(uri_or_rel)
            else _join_uri(self.base_uri, uri_or_rel)
        )
        fs, path = self._fs_and_path(uri)
        with fs.open(path, "r", encoding=encoding) as f:
            return str(f.read())

    def read_json(self, uri_or_rel: str) -> Any:
        """Read and parse JSON from a path."""
        return json.loads(self.read_text(uri_or_rel))
# <<< END: store.py <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

# >>> FILE: merge_ndjson.py >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
#!/usr/bin/env python3
"""Merge daily NDJSON files into a single Tableau-ready file.

Scans all ``<base_dir>/YYYY-MM-DD/results.ndjson`` files, concatenates them
into one ``results_all.ndjson`` (and ``errors_all.ndjson``), and optionally
filters by date range.

Usage::

    # Merge everything in ./output into one file:
    python -m ofam_asset_xfer.merge_ndjson ./output

    # Only last 90 days:
    python -m ofam_asset_xfer.merge_ndjson ./output --days 90

    # Custom output path:
    python -m ofam_asset_xfer.merge_ndjson ./output -o /tmp/tableau_feed

    # Also produce CSV for Tableau Desktop:
    python -m ofam_asset_xfer.merge_ndjson ./output --csv

The merged files are what you point Tableau at — one flat table, all days.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def collect_ndjson_files(
    base_dir: Path,
    filename: str = "results.ndjson",
    after: Optional[date] = None,
) -> List[Path]:
    """Return sorted list of daily NDJSON files, optionally filtered by date."""
    files: List[Path] = []
    if not base_dir.exists():
        return files
    for child in sorted(base_dir.iterdir()):
        if not child.is_dir():
            continue
        try:
            folder_date = date.fromisoformat(child.name)
        except ValueError:
            continue
        if after and folder_date < after:
            continue
        ndjson_file = child / filename
        if ndjson_file.exists():
            files.append(ndjson_file)
    return files


def merge_ndjson_files(files: List[Path]) -> str:
    """Read and concatenate all NDJSON files into one string."""
    parts = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        if text and not text.endswith("\n"):
            text += "\n"
        parts.append(text)
    return "".join(parts)


def ndjson_to_csv(ndjson_text: str) -> str:
    """Convert NDJSON text to CSV text for Tableau Desktop."""
    if not ndjson_text.strip():
        return ""
    rows = [json.loads(line) for line in ndjson_text.strip().split("\n") if line.strip()]
    if not rows:
        return ""
    # Use keys from first row as header (all rows should have same schema)
    fieldnames = list(rows[0].keys())
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def merge(
    base_dir: str,
    out_dir: Optional[str] = None,
    days: Optional[int] = None,
    write_csv: bool = False,
) -> Dict[str, Any]:
    """Merge daily NDJSON into consolidated files.

    Returns a dict with row counts for results and errors.
    """
    base = Path(base_dir)
    dest = Path(out_dir) if out_dir else base
    dest.mkdir(parents=True, exist_ok=True)

    after = date.today() - timedelta(days=days) if days else None

    stats = {}
    for kind in ("results", "errors"):
        files = collect_ndjson_files(base, f"{kind}.ndjson", after=after)
        merged = merge_ndjson_files(files)
        row_count = merged.count("\n")

        ndjson_path = dest / f"{kind}_all.ndjson"
        ndjson_path.write_text(merged, encoding="utf-8")
        log.info("Wrote %d rows to %s (%d files merged)", row_count, ndjson_path, len(files))

        if write_csv and merged.strip():
            csv_path = dest / f"{kind}_all.csv"
            csv_path.write_text(ndjson_to_csv(merged), encoding="utf-8")
            log.info("Wrote CSV to %s", csv_path)

        stats[kind] = {"rows": row_count, "files": len(files)}

    return stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Merge daily NDJSON into a single Tableau-ready file."
    )
    p.add_argument("base_dir", help="Directory containing YYYY-MM-DD/ subfolders.")
    p.add_argument("-o", "--out-dir", default=None, help="Output dir (default: same as base_dir).")
    p.add_argument("--days", type=int, default=None, help="Only include last N days.")
    p.add_argument("--csv", action="store_true", help="Also produce CSV output.")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s — %(message)s",
    )

    stats = merge(args.base_dir, args.out_dir, args.days, args.csv)

    print(f"Results: {stats['results']['rows']} rows from {stats['results']['files']} files")
    print(f"Errors:  {stats['errors']['rows']} rows from {stats['errors']['files']} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
# <<< END: merge_ndjson.py <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

# >>> FILE: bip_client.py >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
"""BI Publisher SOAP client for running reports.

Calls the ExternalReportWSSService SOAP 1.2 endpoint to run a BI Publisher
report and returns the decoded XML output as a list of row dicts.

Authentication uses Bearer token (same as Fusion REST).

Usage:
    cfg = BIPConfig(base_url="https://fa-host.oraclecloud.com",
                    bearer_token="...", report_path="/Custom/.../Report.xdo")
    client = BIPClient(cfg)
    rows = client.run_report(params={"P_BOOK_TYPE_CODE": "US CORP BOOK"})
    # rows = [{"ASSET_NUMBER": "142847", "BOOK_TYPE_CODE": "US CORP BOOK", ...}, ...]
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree as ET

import requests  # type: ignore[import-untyped]

from .exceptions import FusionApiError


log = logging.getLogger(__name__)

# SOAP 1.2 / BIP namespaces
_SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
_PUB_NS = "http://xmlns.oracle.com/oxp/service/PublicReportService"


@dataclass(frozen=True)
class BIPConfig:
    """Configuration for BI Publisher SOAP client."""

    base_url: str  # e.g. https://fa-host.oraclecloud.com
    bearer_token: str  # same Fusion JWT / Bearer token
    report_path: str  # e.g. /Custom/Integrations/Outbound/FA/AssetTransferDFF.xdo
    verify_ssl: bool = True
    timeout_seconds: int = 120

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "BIPConfig":
        """Build a BIPConfig from a raw config dictionary."""
        base_url = str(d.get("base_url", "")).rstrip("/")
        report_path = str(d.get("report_path", "")).strip()

        bearer_token = d.get("bearer_token")
        bearer_token_env = d.get("bearer_token_env")
        if bearer_token_env:
            bearer_token = os.getenv(str(bearer_token_env), bearer_token)

        if not base_url:
            raise ValueError("bip.base_url is required")
        if not bearer_token:
            raise ValueError(
                "bip.bearer_token is required (bearer_token or bearer_token_env)"
            )
        if not report_path:
            raise ValueError("bip.report_path is required")

        return BIPConfig(
            base_url=base_url,
            bearer_token=str(bearer_token),
            report_path=report_path,
            verify_ssl=bool(d.get("verify_ssl", True)),
            timeout_seconds=int(d.get("timeout_seconds", 120)),
        )


class BIPClient:
    """Client for Oracle BI Publisher SOAP reporting service."""

    def __init__(self, cfg: BIPConfig):
        self.cfg = cfg
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {cfg.bearer_token}",
                "Content-Type": "application/soap+xml; charset=utf-8",
            }
        )

    def _endpoint(self) -> str:
        return f"{self.cfg.base_url}/xmlpserver/services/ExternalReportWSSService"

    def run_report(
        self,
        params: Optional[Dict[str, str]] = None,
        report_path: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """Run a BIP report and return parsed rows as list of dicts.

        Args:
            params: Optional report parameters as name -> value.
            report_path: Override the default report path from config.

        Returns:
            List of dicts, one per G_1 row, keyed by XML element names.
        """
        path = report_path or self.cfg.report_path
        report_bytes = self._call_run_report(path, params)
        return self._parse_data_ds(report_bytes)

    def _call_run_report(
        self,
        report_path: str,
        params: Optional[Dict[str, str]],
    ) -> bytes:
        """Build SOAP 1.2 envelope, POST to BIP, return decoded reportBytes."""
        # Build parameter XML fragment
        param_xml = ""
        if params:
            items = []
            for name, value in params.items():
                items.append(
                    "<pub:item>"
                    f"<pub:name>{_xml_escape(name)}</pub:name>"
                    f"<pub:values><pub:item>{_xml_escape(value)}</pub:item></pub:values>"
                    "</pub:item>"
                )
            param_xml = (
                "<pub:parameterNameValues>"
                + "".join(items)
                + "</pub:parameterNameValues>"
            )

        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<soap:Envelope xmlns:soap="{_SOAP_NS}" xmlns:pub="{_PUB_NS}">'
            "<soap:Header/>"
            "<soap:Body>"
            "<pub:runReport>"
            "<pub:reportRequest>"
            f"{param_xml}"
            f"<pub:reportAbsolutePath>{_xml_escape(report_path)}</pub:reportAbsolutePath>"
            "<pub:sizeOfDataChunkDownload>-1</pub:sizeOfDataChunkDownload>"
            "<pub:reportOutputPath/>"
            "</pub:reportRequest>"
            "</pub:runReport>"
            "</soap:Body>"
            "</soap:Envelope>"
        )

        url = self._endpoint()
        log.debug("BIP SOAP POST %s (report=%s)", url, report_path)

        resp = self._session.post(
            url,
            data=envelope.encode("utf-8"),
            timeout=self.cfg.timeout_seconds,
            verify=self.cfg.verify_ssl,
        )

        if resp.status_code >= 400:
            raise FusionApiError(f"BIP SOAP HTTP {resp.status_code}: {resp.text[:500]}")

        return self._extract_report_bytes(resp.content)

    @staticmethod
    def _extract_report_bytes(soap_response: bytes) -> bytes:
        """Extract and base64-decode reportBytes from SOAP response XML."""
        root = ET.fromstring(soap_response)

        # Search for reportBytes element across any namespace
        for elem in root.iter():
            local = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if local == "reportBytes" and elem.text:
                return base64.b64decode(elem.text)

        raise FusionApiError("No reportBytes found in BIP SOAP response")

    @staticmethod
    def _parse_data_ds(data: bytes) -> List[Dict[str, str]]:
        """Parse BIP XML report output structured as DATA_DS/G_1 rows.

        Expected structure:
            <DATA_DS>
              <P_BOOK_TYPE_CODE>UK CORP BOOK</P_BOOK_TYPE_CODE>
              <G_1>
                <ASSET_NUMBER>142847</ASSET_NUMBER>
                <TRANSFER_TO_ENTITY>US Entity</TRANSFER_TO_ENTITY>
                ...
              </G_1>
              ...
            </DATA_DS>

        Root-level scalar elements (like echoed report parameters) are
        injected into every row dict so downstream code can reference them
        without special handling.

        Returns:
            List of dicts, one per G_1 element.
        """
        text = data.decode("utf-8-sig")
        root = ET.fromstring(text)

        # Collect root-level scalar elements (non-G_1 children of DATA_DS).
        # These are typically echoed report parameters like P_BOOK_TYPE_CODE.
        root_fields: Dict[str, str] = {}
        for child in root:
            local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if local != "G_1" and child.text is not None:
                root_fields[local] = child.text.strip()

        rows: List[Dict[str, str]] = []
        # Find all G_1 elements (could be at root level or under DATA_DS)
        g1_elements = root.findall(".//G_1")
        for g1 in g1_elements:
            row: Dict[str, str] = dict(root_fields)  # seed with root-level fields
            for child in g1:
                local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                row[local] = (child.text or "").strip()
            rows.append(row)

        return rows


def _xml_escape(s: str) -> str:
    """Escape XML special characters."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
# <<< END: bip_client.py <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

# >>> FILE: ccid_resolver.py >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
"""Option B: Derive destination EXPENSE_CCID by replacing the Company segment.

Given a source EXPENSE_CCID, look up its GL segments via accountCombinationsLOV,
swap the Company segment to the target value, and find (or fail on) the matching
destination CodeCombinationId.

All HTTP I/O goes through the injected client so this module is unit-testable
with a mock.
"""

from __future__ import annotations

import logging
import re
from copy import deepcopy
from typing import Any, Dict, List, Optional, Protocol, Tuple

from .exceptions import FusionApiError, ValidationError


log = logging.getLogger(__name__)

# Segment fields returned by accountCombinationsLOV (Segment1 .. Segment30).
_SEGMENT_RE = re.compile(r"^Segment\d+$")


# ---------------------------------------------------------------------------
# Protocol for the HTTP client (allows mocking in tests)
# ---------------------------------------------------------------------------
class _RestClient(Protocol):
    def get_resource(
        self, resource_path: str, query_params: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]: ...


# ---------------------------------------------------------------------------
# 1) get_ccid_details
# ---------------------------------------------------------------------------
def get_ccid_details(client: _RestClient, ccid: int) -> Dict[str, Any]:
    """Look up a CodeCombinationId via accountCombinationsLOV PrimaryKey finder.

    Returns::

        {
            "ccid": 626955,
            "chart_of_accounts_id": 12345,          # may be None if not in response
            "concatenated_segments": "US01-100-...", # for logging
            "segments": {"Segment1": "US01", "Segment2": "100", ...},
        }
    """
    log.info("get_ccid_details: looking up CCID=%s", ccid)

    resp = client.get_resource(
        "accountCombinationsLOV",
        {
            "finder": f"PrimaryKey;_CODE_COMBINATION_ID={ccid}",
            "onlyData": "true",
        },
    )

    items = resp.get("items") or []
    if not items:
        raise FusionApiError(
            f"accountCombinationsLOV returned no items for CCID={ccid}. "
            f"Response: {resp}"
        )

    row = items[0]

    segments: Dict[str, str] = {}
    for key, val in row.items():
        if _SEGMENT_RE.match(key) and val is not None and str(val).strip():
            segments[key] = str(val).strip()

    if not segments:
        raise FusionApiError(
            f"No Segment* fields found in accountCombinationsLOV response for CCID={ccid}. "
            f"Row keys: {list(row.keys())}"
        )

    coa_id = row.get("ChartOfAccountsId") or row.get("chartOfAccountsId")
    concat_segs = (
        row.get("ConcatenatedSegments") or row.get("concatenatedSegments") or ""
    )

    result = {
        "ccid": ccid,
        "chart_of_accounts_id": int(coa_id) if coa_id else None,
        "concatenated_segments": str(concat_segs),
        "segments": segments,
    }

    log.info(
        "get_ccid_details: CCID=%s → coa=%s segments=%s concatenated=%s",
        ccid,
        result["chart_of_accounts_id"],
        segments,
        concat_segs,
    )
    return result


# ---------------------------------------------------------------------------
# 2) build_target_segments
# ---------------------------------------------------------------------------
def build_target_segments(
    src_segments: Dict[str, str],
    target_company: str,
    company_segment_key: str = "Segment1",
) -> Dict[str, str]:
    """Copy source segments and replace the Company segment with *target_company*.

    Raises ValidationError if *company_segment_key* is not present in
    *src_segments*.
    """
    if company_segment_key not in src_segments:
        raise ValidationError(
            f"Company segment key '{company_segment_key}' not found in source segments. "
            f"Available: {sorted(src_segments.keys())}"
        )

    target = deepcopy(src_segments)
    old_val = target[company_segment_key]
    target[company_segment_key] = target_company

    log.info(
        "build_target_segments: %s changed from '%s' to '%s'  (other segments unchanged)",
        company_segment_key,
        old_val,
        target_company,
    )
    return target


# ---------------------------------------------------------------------------
# 3) lookup_ccid_by_segments
# ---------------------------------------------------------------------------
def lookup_ccid_by_segments(
    client: _RestClient,
    target_segments: Dict[str, str],
    chart_of_accounts_id: Optional[int] = None,
) -> int:
    """Find an existing CodeCombinationId that matches all *target_segments*.

    Uses accountCombinationsLOV with a ``q`` filter.  If no match is found,
    raises a hard error with a clear message about the missing combination.
    """
    # Build q= filter:  Segment1='US01';Segment2='100';...
    # Sort segment keys numerically (Segment1, Segment2, ..., Segment10, ...)
    def _seg_sort_key(k: str) -> Tuple[Any, ...]:
        m = re.match(r"^(Segment)(\d+)$", k)
        if m:
            return (0, int(m.group(2)))
        return (1, k)

    sorted_keys = sorted(target_segments.keys(), key=_seg_sort_key)

    parts: List[str] = []
    for key in sorted_keys:
        parts.append(f"{key}={target_segments[key]}")
    if chart_of_accounts_id is not None:
        parts.append(f"ChartOfAccountsId={chart_of_accounts_id}")

    q_filter = ";".join(parts)

    # Request the segment fields we need for exact-match verification
    segment_fields = ",".join(sorted_keys)
    fields = (
        f"CodeCombinationId,ConcatenatedSegments,ChartOfAccountsId,{segment_fields}"
    )

    log.info("lookup_ccid_by_segments: q=%s fields=%s", q_filter, fields)

    resp = client.get_resource(
        "accountCombinationsLOV",
        {
            "q": q_filter,
            "onlyData": "true",
            "fields": fields,
        },
    )

    items = resp.get("items") or []

    if not items:
        segs_display = ", ".join(f"{k}={v}" for k, v in sorted(target_segments.items()))
        raise ValidationError(
            f"No account combination found for target segments: {segs_display}. "
            f"ChartOfAccountsId={chart_of_accounts_id}. "
            f"The combination may need to be created in Oracle GL first."
        )

    # If multiple matches, pick exact match (all segments equal).
    for row in items:
        match = True
        for key, expected in target_segments.items():
            actual = str(row.get(key, "")).strip()
            if actual != expected:
                match = False
                break
        if match:
            target_ccid = int(row["CodeCombinationId"])
            log.info(
                "lookup_ccid_by_segments: found exact match CCID=%s (%s)",
                target_ccid,
                row.get("ConcatenatedSegments", ""),
            )
            return target_ccid

    # Fallback: first item (if segments were not returned in response, trust the filter)
    target_ccid = int(items[0]["CodeCombinationId"])
    log.warning(
        "lookup_ccid_by_segments: could not verify exact segment match; "
        "using first result CCID=%s. items_count=%d",
        target_ccid,
        len(items),
    )
    return target_ccid


# ---------------------------------------------------------------------------
# 4) Top-level convenience: resolve_target_expense_ccid
# ---------------------------------------------------------------------------
def resolve_target_expense_ccid(
    client: _RestClient,
    src_expense_ccid: int,
    target_company: str,
    company_segment_key: str = "Segment1",
) -> int:
    """End-to-end: given a source expense CCID and a target Company value,
    return the destination expense CCID.

    Raises:
        ValidationError  if target_ccid == src_expense_ccid (would create
            identical distribution lines).
    """
    details = get_ccid_details(client, src_expense_ccid)
    src_segments = details["segments"]
    coa_id = details["chart_of_accounts_id"]

    target_segments = build_target_segments(
        src_segments, target_company, company_segment_key
    )

    target_ccid = lookup_ccid_by_segments(client, target_segments, coa_id)

    if target_ccid == src_expense_ccid:
        raise ValidationError(
            f"Target CCID ({target_ccid}) equals source CCID ({src_expense_ccid}); "
            f"would create identical distribution lines. "
            f"Check target_company='{target_company}' vs source "
            f"{company_segment_key}='{src_segments.get(company_segment_key)}'."
        )

    log.info(
        "resolve_target_expense_ccid: src=%s → target=%s (company %s→%s)",
        src_expense_ccid,
        target_ccid,
        src_segments.get(company_segment_key),
        target_company,
    )
    return target_ccid
# <<< END: ccid_resolver.py <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

# >>> FILE: oracle_client.py >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
import requests  # type: ignore[import-untyped]

from .exceptions import FusionApiError
from .paramlist import build_parameter_list


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OracleConfig:
    base_url: str
    api_version: str
    bearer_token: str
    verify_ssl: bool = True
    timeout_seconds: int = 60

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "OracleConfig":
        """Build an OracleConfig from a raw config dictionary."""
        base_url = str(d.get("base_url", "")).rstrip("/")
        api_version = str(d.get("api_version", "")).strip()
        if not base_url or not api_version:
            raise ValueError("oracle.base_url and oracle.api_version are required")

        # Prefer env indirection for token (CDX secrets pattern)
        bearer_token = d.get("bearer_token")
        bearer_token_env = d.get("bearer_token_env")

        if bearer_token_env:
            bearer_token = os.getenv(str(bearer_token_env), bearer_token)

        if not bearer_token:
            raise ValueError(
                "Oracle bearer_token is required (bearer_token or bearer_token_env)."
            )

        return OracleConfig(
            base_url=base_url,
            api_version=api_version,
            bearer_token=str(bearer_token),
            verify_ssl=bool(d.get("verify_ssl", True)),
            timeout_seconds=int(d.get("timeout_seconds", 60)),
        )


class OracleErpIntegrationsClient:
    """Client for Oracle Fusion ERP Integration REST Service for Assets transactions."""

    def __init__(self, cfg: OracleConfig):
        self.cfg = cfg
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {cfg.bearer_token}",
                "Content-Type": "application/vnd.oracle.adf.resourceitem+json",
                "REST-header-version": "4",
                "ACCEPT": "application/json",
            }
        )

    def _endpoint(self, handle: str) -> str:
        # Example from doc: /fscmRestApi/resources/11.13.18.05/erpintegrations/processTransaction-transferAsset
        rel = f"/fscmRestApi/resources/{self.cfg.api_version}/erpintegrations/processTransaction-{handle}"
        return self.cfg.base_url + rel

    def _resource_url(self, resource_path: str) -> str:
        rel = f"/fscmRestApi/resources/{self.cfg.api_version}/{resource_path}"
        return self.cfg.base_url + rel

    def process_transaction(
        self, handle: str, params: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """POST processTransaction-<handle>.

        Returns:
          (raw_response_json, parsed_parameter_list_dict)
        """
        op_name = f"processTransaction-{handle}"
        payload = {
            "OperationName": op_name,
            "ParameterList": build_parameter_list(params),
        }

        url = self._endpoint(handle)
        log.debug("POST %s payload=%s", url, payload)

        r = self._session.post(
            url,
            json=payload,
            timeout=self.cfg.timeout_seconds,
            verify=self.cfg.verify_ssl,
        )
        try:
            raw = r.json()
        except Exception as e:
            raise FusionApiError(
                f"Non-JSON response from Fusion (status={r.status_code}): {r.text[:500]}"
            ) from e

        if r.status_code >= 400:
            raise FusionApiError(f"Fusion HTTP {r.status_code}: {raw}")

        pl_raw = raw.get("ParameterList")
        pl: Dict[str, Any] = {}
        if isinstance(pl_raw, str) and pl_raw.strip():
            try:
                pl = json.loads(pl_raw)
            except Exception:
                # Some handles may return a non-JSON string ParameterList. Preserve it.
                pl = {"_raw": pl_raw}
        elif isinstance(pl_raw, dict):
            pl = pl_raw

        return raw, pl

    def get_resource(
        self, resource_path: str, query_params: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """GET a Fusion REST resource (e.g. accountCombinationsLOV).

        Returns the parsed JSON response body.
        """
        url = self._resource_url(resource_path)
        log.debug("GET %s params=%s", url, query_params)

        r = self._session.get(
            url,
            params=query_params or {},
            timeout=self.cfg.timeout_seconds,
            verify=self.cfg.verify_ssl,
        )
        try:
            raw = r.json()
        except Exception as e:
            raise FusionApiError(
                f"Non-JSON response from Fusion GET (status={r.status_code}): {r.text[:500]}"
            ) from e

        if r.status_code >= 400:
            raise FusionApiError(f"Fusion GET HTTP {r.status_code}: {raw}")

        return dict(raw)
# <<< END: oracle_client.py <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

# >>> FILE: gcs_publisher.py >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
"""Publish transfer results to GCS in a Tableau-friendly NDJSON format.

Each run **appends** flat rows to daily files::

    gs://<bucket>/<prefix>/YYYY-MM-DD/results.ndjson
    gs://<bucket>/<prefix>/YYYY-MM-DD/errors.ndjson   (FAILED rows only)

Every row is a self-contained JSON object with ``run_date``, ``run_ts``,
and ``dry_run`` stamped in, so Tableau / BigQuery can query without joins.

On each publish the publisher also **prunes** blobs whose date-folder is
older than ``retention_days`` (default 365).

Configuration
-------------
::

    {
      "gcs": {
        "bucket": "my-fa-transfer-logs",
        "prefix": "transfers",
        "service_account_file": "/path/to/sa-key.json",
        "service_account_file_env": "GCS_SA_KEY_PATH",
        "retention_days": 365
      }
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Dict, List

from .exceptions import ConfigError

log = logging.getLogger(__name__)

# Matches date-named folders like  transfers/2025-03-09/
_DATE_FOLDER_RE = re.compile(r"(\d{4}-\d{2}-\d{2})/")


def _flatten_result(
    row: Dict[str, Any],
    run_date: str,
    run_ts: int,
    dry_run: bool,
) -> Dict[str, Any]:
    """Flatten a TransferResult dict into a Tableau-friendly row.

    Removes nested ``fusion_response`` (too large / variable for dashboards)
    and stamps run metadata onto every row.
    """
    flat: Dict[str, Any] = {
        "run_date": run_date,
        "run_ts": run_ts,
        "dry_run": dry_run,
        "asset_number": row.get("asset_number"),
        "status": row.get("status"),
        "source_book": row.get("source_book"),
        "target_book": row.get("target_book"),
        "transfer_to_entity": row.get("transfer_to_entity"),
        "transfer_date": row.get("transfer_date"),
        "error": row.get("error"),
    }
    return flat


def flatten_results(
    results: List[Dict[str, Any]],
    run_date: str,
    run_ts: int,
    dry_run: bool,
) -> List[Dict[str, Any]]:
    """Convert raw results into flat Tableau-ready rows."""
    return [_flatten_result(r, run_date, run_ts, dry_run) for r in results]


def to_ndjson(rows: List[Dict[str, Any]]) -> str:
    """Serialise rows as newline-delimited JSON (one JSON object per line)."""
    return "".join(json.dumps(r, sort_keys=True, default=str) + "\n" for r in rows)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class GCSPublisherConfig:
    """Immutable config for :class:`GCSResultPublisher`."""

    bucket: str
    prefix: str
    service_account_file: str
    retention_days: int = 365

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "GCSPublisherConfig":
        """Build a GCSPublisherConfig from a raw config dict."""
        bucket = d.get("bucket", "").strip()
        if not bucket:
            raise ConfigError("gcs.bucket is required")
        # Strip gs:// scheme if provided (users often copy the full URI).
        if bucket.startswith("gs://"):
            bucket = bucket[len("gs://"):]
        bucket = bucket.strip("/")

        prefix = d.get("prefix", "transfers").strip().strip("/")

        sa_file = d.get("service_account_file", "").strip()
        if not sa_file:
            env_key = d.get("service_account_file_env", "").strip()
            if env_key:
                sa_file = os.environ.get(env_key, "").strip()
            if not sa_file:
                raise ConfigError(
                    "gcs.service_account_file or gcs.service_account_file_env is required"
                )
        # Expand ~ to the user's home directory.
        sa_file = os.path.expanduser(sa_file)

        retention_days = int(d.get("retention_days", 365))

        return cls(
            bucket=bucket,
            prefix=prefix,
            service_account_file=sa_file,
            retention_days=retention_days,
        )


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------
class GCSResultPublisher:
    """Appends transfer results as NDJSON to daily files on GCS.

    Implements the :class:`~ofam_asset_xfer.result_publisher.ResultPublisher`
    protocol — no base-class coupling.
    """

    def __init__(self, config: GCSPublisherConfig) -> None:
        self._cfg = config
        self._client = None  # lazy

    def _get_client(self) -> Any:
        """Lazy-init the GCS client so import-time doesn't require google libs."""
        if self._client is None:
            from google.cloud import storage  # type: ignore[import-untyped]

            self._client = storage.Client.from_service_account_json(
                self._cfg.service_account_file
            )
        return self._client

    # -- append -------------------------------------------------------------

    def _append_ndjson(self, blob_path: str, ndjson_text: str) -> str:
        """Append *ndjson_text* to an existing blob, or create it."""
        client = self._get_client()
        bucket = client.bucket(self._cfg.bucket)
        blob = bucket.blob(blob_path)

        existing = ""
        if blob.exists():
            existing = blob.download_as_text(encoding="utf-8")

        merged = existing + ndjson_text
        blob.upload_from_string(merged, content_type="application/x-ndjson")

        uri = f"gs://{self._cfg.bucket}/{blob_path}"
        log.info("Appended %d row(s) to %s", ndjson_text.count("\n"), uri)
        return uri

    # -- prune --------------------------------------------------------------

    def _prune_old_blobs(self) -> int:
        """Delete blobs in date-folders older than retention_days."""
        cutoff = date.today() - timedelta(days=self._cfg.retention_days)
        client = self._get_client()
        bucket = client.bucket(self._cfg.bucket)
        prefix = self._cfg.prefix + "/"

        deleted = 0
        for blob in bucket.list_blobs(prefix=prefix):
            match = _DATE_FOLDER_RE.search(blob.name[len(prefix):])
            if not match:
                continue
            try:
                folder_date = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if folder_date < cutoff:
                blob.delete()
                deleted += 1

        if deleted:
            log.info("Pruned %d blob(s) older than %s", deleted, cutoff.isoformat())
        return deleted

    # -- ResultPublisher protocol -------------------------------------------

    def publish(self, summary: Dict[str, Any], results: List[Dict[str, Any]]) -> None:
        """Flatten, append to daily NDJSON files, and prune old data."""
        run_date = date.today().isoformat()
        run_ts = int(time.time())
        dry_run = summary.get("dry_run", True)

        flat = flatten_results(results, run_date, run_ts, dry_run)
        day_prefix = f"{self._cfg.prefix}/{run_date}"

        if flat:
            self._append_ndjson(f"{day_prefix}/results.ndjson", to_ndjson(flat))

        errors = [r for r in flat if r.get("status") == "FAILED"]
        if errors:
            self._append_ndjson(f"{day_prefix}/errors.ndjson", to_ndjson(errors))
            log.warning("Published %d error(s)", len(errors))
        else:
            log.info("No errors to publish")

        try:
            self._prune_old_blobs()
        except Exception:
            log.exception("Prune failed (non-fatal)")
# <<< END: gcs_publisher.py <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

# >>> FILE: fusion_sync.py >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
"""Fusion FA DFF-based IU transfer discovery and execution.

Replaces the CMDB-based sync approach.  Instead of querying ServiceNow CMDB
for asset locations, we read the asset-level Descriptive Flexfields (DFFs)
from an Oracle BI Publisher report (All_IUT_Transfers_Rpt):

  * **Book Type Code**              – P_BOOK_TYPE_CODE              – echoed report parameter at root level
  * **Transfer Date**               – TRANSFER_DATE                 – when the IU transfer should happen
  * **Transfer to Entity**          – TRANSFER_TO_ENTITY            – the destination legal entity
  * **Transfer to Location**        – TRANSFER_TO_LOCATION          – optional location hint
  * **Final Target Book Type Code** – FINAL_TARGET_BOOK_TYPE_CODE   – pre-resolved target book from report
  * **Target Location ID**          – TARGET_LOCATION_ID            – target location ID from report

Architecture:
  1. Call BIP report via SOAP to get asset transfer candidates (DFF populated)
  2. For each candidate, call getAssetInformation to get full asset state
  3. Use FINAL_TARGET_BOOK_TYPE_CODE from report as target book (fallback: resolve entity → target book via EntityBookResolver)
  4. Compare current book vs target book
  5. If book mismatch → cross-book transfer via processTransaction-bookTransfer
  6. If same book but location differs → same-book transfer via processTransaction-transferAsset
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Set

from .bip_client import BIPClient
from .entity_resolver import EntityBookResolver
from .exceptions import FusionApiError, ValidationError
from .fusion_ops import (
    AssetState,
    build_book_transfer_params,
    build_same_book_transfer_params,
    get_asset_information,
)
from .oracle_client import OracleErpIntegrationsClient

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DFF column configuration (maps BIP report column names to logical fields)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DFFConfig:
    """Maps logical DFF fields to BIP report XML column names.

    The column names depend on the SQL aliases used in the BIP report
    data model.  Override these if your report uses different names.
    """

    # Asset identity columns in the BIP report
    asset_id_col: str = "ASSET_ID"
    asset_number_col: str = "ASSET_NUMBER"
    book_type_code_col: str = "P_BOOK_TYPE_CODE"

    # All_IUT_Transfers_Rpt column aliases
    transfer_date_col: str = "TRANSFER_DATE"
    transfer_to_entity_col: str = "TRANSFER_TO_ENTITY"
    transfer_to_location_col: str = "TRANSFER_TO_LOCATION"

    # Pre-resolved target fields from report (preferred over entity resolution)
    final_target_book_type_code_col: str = "FINAL_TARGET_BOOK_TYPE_CODE"
    target_location_id_col: str = "TARGET_LOCATION_ID"
    current_location_id_col: str = "CURRENT_LOCATION_ID"
    target_expense_ccid_col: str = "TARGET_EXPENSE_CCID"


DEFAULT_DFF_CONFIG = DFFConfig()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class PendingTransfer:
    """An FA asset whose DFF indicates a pending IU transfer."""

    # FA identity
    asset_id: str
    asset_number: str
    book_type_code: str  # current book
    description: Optional[str]
    tag_number: Optional[str]
    cost: Optional[str]

    # From DFF
    transfer_date: str  # effective date from DFF
    transfer_to_entity: str  # destination entity from DFF
    transfer_to_location: Optional[str]  # optional location hint from DFF

    # Resolved target
    target_book_type_code: str  # from FINAL_TARGET_BOOK_TYPE_CODE or entity resolver
    target_location_id: Optional[str] = None  # from TARGET_LOCATION_ID in report
    target_expense_ccid: Optional[str] = None  # from TARGET_EXPENSE_CCID in report
    is_cross_book: bool = True  # False = same-book (location-only) transfer

    # Cached state for transfer execution
    fa_state: Optional[AssetState] = field(default=None, repr=False)


@dataclass
class TransferResult:
    """Result of a single IU transfer operation."""

    asset_number: str
    status: str  # TRANSFERRED, NOOP, DRY_RUN, FAILED
    source_book: Optional[str] = None
    target_book: Optional[str] = None
    transfer_to_entity: Optional[str] = None
    transfer_date: Optional[str] = None
    error: Optional[str] = None
    fusion_response: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# FusionIUSync — core engine
# ---------------------------------------------------------------------------
class FusionIUSync:
    """Discovers and executes IU transfers using FA asset DFF fields."""

    def __init__(
        self,
        fusion_client: OracleErpIntegrationsClient,
        entity_resolver: EntityBookResolver,
        bip_client: BIPClient,
        dff_config: Optional[DFFConfig] = None,
    ):
        self._client = fusion_client
        self._entity_resolver = entity_resolver
        self._bip = bip_client
        self._dff = dff_config or DEFAULT_DFF_CONFIG

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def find_pending_transfers(
        self,
        books: List[str],
        limit: int = 100,
        bip_params: Optional[Dict[str, str]] = None,
    ) -> List[PendingTransfer]:
        """Discover pending IU transfers from BIP report + getAssetInformation.

        1. Run the BIP report to get assets with Transfer-to-Entity DFF populated.
        2. For each candidate, call getAssetInformation to get full asset state.
        3. Resolve entity → target book and filter for actual pending transfers.

        Args:
            books: List of book_type_codes to scan.
            limit: Max pending transfers to collect.
            bip_params: Optional extra parameters to pass to the BIP report.

        Returns:
            List of PendingTransfer objects (at most ``limit``).
        """
        log.info("Scanning for pending IU transfers (books=%s, limit=%d)", books, limit)
        books_upper: Set[str] = {b.upper().strip() for b in books}

        # Step 1: Run BIP report per book to get transfer candidates.
        # The BIP report requires P_BOOK_TYPE_CODE to filter assets.
        rows: List[Dict[str, str]] = []
        for book in books:
            report_params = dict(bip_params or {})
            report_params["P_BOOK_TYPE_CODE"] = book
            try:
                book_rows = self._bip.run_report(params=report_params)
                log.info("BIP report for book '%s' returned %d row(s)", book, len(book_rows))
                rows.extend(book_rows)
            except FusionApiError as e:
                log.error("BIP report failed for book '%s': %s", book, e)

        log.info("BIP report returned %d total row(s) across %d book(s)", len(rows), len(books))

        # Step 2: Filter and enrich each candidate
        pending: List[PendingTransfer] = []
        for row in rows:
            if len(pending) >= limit:
                break

            result = self._check_bip_row(row, books_upper)
            if result:
                pending.append(result)
                log.info(
                    "Pending #%d: asset=%s, %s → %s (entity=%s, date=%s)",
                    len(pending),
                    result.asset_number,
                    result.book_type_code,
                    result.target_book_type_code,
                    result.transfer_to_entity,
                    result.transfer_date,
                )

        log.info("Scan complete: found %d pending transfer(s)", len(pending))
        return pending

    def _check_bip_row(
        self,
        row: Dict[str, str],
        books_upper: set,
    ) -> Optional[PendingTransfer]:
        """Check a single BIP report row for a pending transfer.

        Calls getAssetInformation to get full asset state for validated
        candidates.
        """
        dff = self._dff

        # --- Extract DFF fields from report row ---
        transfer_entity = row.get(dff.transfer_to_entity_col, "").strip()
        if not transfer_entity:
            return None

        asset_id = row.get(dff.asset_id_col, "").strip()
        asset_number = row.get(dff.asset_number_col, "").strip()
        book_type_code = row.get(dff.book_type_code_col, "").strip()
        if not asset_number or not book_type_code:
            return None

        # Check book is in the scan list
        if book_type_code.upper().strip() not in books_upper:
            return None

        transfer_date_raw = row.get(dff.transfer_date_col, "").strip()
        transfer_location = row.get(dff.transfer_to_location_col, "").strip() or None

        # --- Resolve target book ---
        # Prefer FINAL_TARGET_BOOK_TYPE_CODE from the report (pre-resolved).
        # Fall back to entity resolver if not present.
        final_target_book = row.get(dff.final_target_book_type_code_col, "").strip()
        target_location_id = row.get(dff.target_location_id_col, "").strip() or None
        current_location_id = row.get(dff.current_location_id_col, "").strip() or None
        target_expense_ccid = row.get(dff.target_expense_ccid_col, "").strip() or None

        # --- Location already matches → skip ---
        if current_location_id and target_location_id and current_location_id == target_location_id:
            log.info(
                "Skipping asset %s: CURRENT_LOCATION_ID (%s) already equals "
                "TARGET_LOCATION_ID (%s) — no transfer needed",
                asset_number,
                current_location_id,
                target_location_id,
            )
            return None

        if final_target_book:
            target_book = final_target_book
            log.debug(
                "Using FINAL_TARGET_BOOK_TYPE_CODE='%s' for asset %s",
                target_book,
                asset_number,
            )
        else:
            try:
                target_book = self._entity_resolver.resolve_target_book(transfer_entity)
            except ValidationError as e:
                log.debug("Skipping asset %s: %s", asset_number, e)
                return None

        is_cross_book = book_type_code.upper().strip() != target_book.upper().strip()

        # Same book AND no target location/expense info → nothing to transfer
        if not is_cross_book and not target_location_id and not target_expense_ccid:
            return None

        # --- Call getAssetInformation to get full state ---
        try:
            _raw, _pl, state = get_asset_information(
                self._client,
                book_type_code,
                asset_number,
                asset_id=asset_id or None,
            )
        except FusionApiError as e:
            log.warning(
                "getAssetInformation failed for asset=%s book=%s: %s",
                asset_number,
                book_type_code,
                e,
            )
            return None

        return PendingTransfer(
            asset_id=state.asset_id,
            asset_number=state.asset_number,
            book_type_code=book_type_code,
            description=state.description,
            tag_number=state.tag_number,
            cost=state.cost,
            transfer_date=transfer_date_raw or date.today().isoformat(),
            transfer_to_entity=transfer_entity,
            transfer_to_location=transfer_location,
            target_book_type_code=target_book,
            target_location_id=target_location_id,
            target_expense_ccid=target_expense_ccid,
            is_cross_book=is_cross_book,
            fa_state=state,
        )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def execute_transfer(
        self,
        pending: PendingTransfer,
        dry_run: bool = True,
    ) -> TransferResult:
        """Execute an IU transfer for a pending asset.

        Uses the DFF Transfer Date as the effective date.

        Args:
            pending: The pending transfer to execute.
            dry_run: If True, build payload but don't POST.

        Returns:
            TransferResult with transfer outcome.
        """
        effective_date = pending.transfer_date or date.today().isoformat()
        request_id = f"IU_XFER_{pending.asset_number}_{int(time.time())}"

        # State should already be cached from discovery
        state = pending.fa_state
        if not state:
            try:
                _raw, _pl, state = get_asset_information(
                    self._client,
                    pending.book_type_code,
                    pending.asset_number,
                )
            except FusionApiError as e:
                return TransferResult(
                    asset_number=pending.asset_number,
                    status="FAILED",
                    source_book=pending.book_type_code,
                    target_book=pending.target_book_type_code,
                    transfer_to_entity=pending.transfer_to_entity,
                    transfer_date=effective_date,
                    error=f"Failed to get asset state: {e}",
                )

        try:
            if pending.is_cross_book:
                return self._execute_cross_book(
                    pending, state, request_id, effective_date, dry_run,
                )
            else:
                return self._execute_same_book(
                    pending, state, request_id, effective_date, dry_run,
                )
        except Exception as e:
            log.exception("Transfer failed for asset=%s", pending.asset_number)
            return TransferResult(
                asset_number=pending.asset_number,
                status="FAILED",
                source_book=pending.book_type_code,
                target_book=pending.target_book_type_code,
                transfer_to_entity=pending.transfer_to_entity,
                transfer_date=effective_date,
                error=str(e),
            )

    def _execute_cross_book(
        self,
        pending: PendingTransfer,
        state: AssetState,
        request_id: str,
        effective_date: str,
        dry_run: bool,
    ) -> TransferResult:
        """Execute a cross-book IU transfer."""
        log.info(
            "Cross-book transfer: asset=%s, %s → %s (entity=%s)",
            pending.asset_number,
            pending.book_type_code,
            pending.target_book_type_code,
            pending.transfer_to_entity,
        )

        overrides: Dict[str, Any] = {}
        # Prefer TARGET_LOCATION_ID from report; fall back to TRANSFER_TO_LOCATION DFF
        location_override = pending.target_location_id or pending.transfer_to_location
        if location_override:
            overrides["location_ccid"] = location_override
        if pending.target_expense_ccid:
            overrides["expense_ccid"] = pending.target_expense_ccid

        params = build_book_transfer_params(
            state=state,
            dest_book_type_code=pending.target_book_type_code,
            effective_date=effective_date,
            overrides=overrides,
            request_id=request_id,
        )

        if dry_run:
            log.info(
                "DRY-RUN: IU transfer payload built for asset=%s", pending.asset_number
            )
            return TransferResult(
                asset_number=pending.asset_number,
                status="DRY_RUN",
                source_book=pending.book_type_code,
                target_book=pending.target_book_type_code,
                transfer_to_entity=pending.transfer_to_entity,
                transfer_date=effective_date,
                fusion_response={"planned_params": params},
            )

        raw, pl = self._client.process_transaction("bookTransfer", params)
        status_code = str(pl.get("X_RETURN_STATUS") or "").strip()

        return TransferResult(
            asset_number=pending.asset_number,
            status="TRANSFERRED" if status_code == "S" else "FAILED",
            source_book=pending.book_type_code,
            target_book=pending.target_book_type_code,
            transfer_to_entity=pending.transfer_to_entity,
            transfer_date=effective_date,
            fusion_response=pl,
            error=None
            if status_code == "S"
            else f"Fusion X_RETURN_STATUS={status_code}",
        )

    def _execute_same_book(
        self,
        pending: PendingTransfer,
        state: AssetState,
        request_id: str,
        effective_date: str,
        dry_run: bool,
    ) -> TransferResult:
        """Execute a same-book transfer (location/distribution change)."""
        log.info(
            "Same-book transfer: asset=%s, book=%s, target_location=%s (entity=%s)",
            pending.asset_number,
            pending.book_type_code,
            pending.target_location_id,
            pending.transfer_to_entity,
        )

        overrides: Dict[str, Any] = {}
        location_override = pending.target_location_id or pending.transfer_to_location
        if location_override:
            overrides["location_ccid"] = location_override
        if pending.target_expense_ccid:
            overrides["expense_ccid"] = pending.target_expense_ccid

        params, is_noop = build_same_book_transfer_params(
            state=state,
            effective_date=effective_date,
            overrides=overrides,
            request_id=request_id,
        )

        if is_noop:
            log.info(
                "NOOP: no distribution changes for asset=%s", pending.asset_number
            )
            return TransferResult(
                asset_number=pending.asset_number,
                status="NOOP",
                source_book=pending.book_type_code,
                target_book=pending.target_book_type_code,
                transfer_to_entity=pending.transfer_to_entity,
                transfer_date=effective_date,
            )

        if dry_run:
            log.info(
                "DRY-RUN: same-book transfer payload built for asset=%s",
                pending.asset_number,
            )
            return TransferResult(
                asset_number=pending.asset_number,
                status="DRY_RUN",
                source_book=pending.book_type_code,
                target_book=pending.target_book_type_code,
                transfer_to_entity=pending.transfer_to_entity,
                transfer_date=effective_date,
                fusion_response={"planned_params": params},
            )

        raw, pl = self._client.process_transaction("transferAsset", params)
        status_code = str(pl.get("X_RETURN_STATUS") or "").strip()

        return TransferResult(
            asset_number=pending.asset_number,
            status="TRANSFERRED" if status_code == "S" else "FAILED",
            source_book=pending.book_type_code,
            target_book=pending.target_book_type_code,
            transfer_to_entity=pending.transfer_to_entity,
            transfer_date=effective_date,
            fusion_response=pl,
            error=None
            if status_code == "S"
            else f"Fusion X_RETURN_STATUS={status_code}",
        )

    # ------------------------------------------------------------------
    # Batch entry point
    # ------------------------------------------------------------------
    def run_full_sync(
        self,
        books: List[str],
        dry_run: bool = True,
        max_transfers: int = 500,
        bip_params: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Production entry point: discover pending IU transfers via BIP, execute.

        Returns:
            Summary dict with counts and per-asset results.
        """
        started = int(time.time())

        log.info(
            "=== Fusion IU Sync started (books=%s, dry_run=%s) ===",
            books,
            dry_run,
        )

        pending_list = self.find_pending_transfers(
            books=books,
            limit=max_transfers,
            bip_params=bip_params,
        )

        results: List[Dict[str, Any]] = []
        counts = {
            "total": len(pending_list),
            "transferred": 0,
            "failed": 0,
            "dry_run": 0,
        }

        for pt in pending_list:
            result = self.execute_transfer(pt, dry_run=dry_run)
            results.append(
                {
                    "asset_number": result.asset_number,
                    "status": result.status,
                    "source_book": result.source_book,
                    "target_book": result.target_book,
                    "transfer_to_entity": result.transfer_to_entity,
                    "transfer_date": result.transfer_date,
                    "error": result.error,
                    "fusion_response": result.fusion_response,
                }
            )
            status_key = result.status.lower()
            if status_key in counts:
                counts[status_key] += 1

        finished = int(time.time())

        summary = {
            "started_ts": started,
            "finished_ts": finished,
            "duration_seconds": finished - started,
            "dry_run": dry_run,
            "books": books,
            "counts": counts,
            "results": results,
        }

        log.info("=== Fusion IU Sync complete: %s ===", counts)
        return summary
# <<< END: fusion_sync.py <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

# >>> FILE: job.py >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from .bip_client import BIPClient, BIPConfig
from .entity_resolver import EntityBookResolver
from .exceptions import ConfigError, FusionApiError
from .fusion_sync import FusionIUSync, DFFConfig
from .oracle_client import OracleConfig, OracleErpIntegrationsClient
from .gcs_publisher import GCSPublisherConfig, GCSResultPublisher
from .local_publisher import LocalResultPublisher
from .store import ArtifactStore
from .fusion_ops import (
    get_asset_information,
    build_same_book_transfer_params,
    build_same_book_transfer_params_option_b,
    build_book_transfer_params,
    build_add_asset_params,
    build_retire_asset_params,
)
from .template import render


log = logging.getLogger(__name__)


def _load_config(config_uri: str) -> Dict[str, Any]:
    store = ArtifactStore(base_uri=".")
    try:
        raw = store.read_text(config_uri)
    except Exception as e:
        raise ConfigError(f"Failed to read config: {config_uri}") from e
    try:
        result: Dict[str, Any] = json.loads(raw)
        return result
    except Exception as e:
        raise ConfigError(f"Config is not valid JSON: {config_uri}") from e


def _validate_request(req: Dict[str, Any]) -> None:
    if not req.get("request_id"):
        raise ConfigError("Each request must include request_id.")
    if not isinstance(req.get("source"), dict):
        raise ConfigError(
            "Each request must include source {book_type_code, asset_number}."
        )
    src = req["source"]
    if not src.get("book_type_code") or not src.get("asset_number"):
        raise ConfigError("source.book_type_code and source.asset_number are required.")
    t = str(req.get("transfer_type", "")).upper()
    if t not in ("SAME_BOOK", "XBOOK"):
        raise ConfigError("transfer_type must be SAME_BOOK or XBOOK.")
    if t == "XBOOK":
        if not isinstance(req.get("target"), dict) and not isinstance(
            req.get("xbook"), dict
        ):
            raise ConfigError(
                "XBOOK requires either target{book_type_code,...} or xbook{...} configuration."
            )


def _run_bip_flow(
    config: Dict[str, Any], store: ArtifactStore, execute: bool
) -> None:
    """BIP-driven flow: discover transfers from 'books' config via BIP report."""
    oracle_cfg = OracleConfig.from_dict(config.get("oracle", {}))
    fusion_client = OracleErpIntegrationsClient(oracle_cfg)

    bip_cfg = BIPConfig.from_dict(config.get("bip", {}))
    bip_client = BIPClient(bip_cfg)

    entity_map = config.get("entity_book_map", {})
    if not entity_map:
        raise ConfigError("Config must include non-empty 'entity_book_map'.")
    entity_resolver = EntityBookResolver(entity_map)

    books = config.get("books", [])

    dff_config = None
    if config.get("dff_columns"):
        dff_config = DFFConfig(**config["dff_columns"])

    bip_params = config.get("bip_params")
    max_transfers = int(config.get("max_transfers", 500))
    dry_run = not execute

    sync = FusionIUSync(
        fusion_client,
        entity_resolver,
        bip_client,
        dff_config=dff_config,
    )
    summary = sync.run_full_sync(
        books=books,
        dry_run=dry_run,
        max_transfers=max_transfers,
        bip_params=bip_params,
    )

    store.write_json("summary.json", summary)
    store.write_json("results.json", summary.get("results", []))

    # Publish Tableau-friendly NDJSON (always local, optionally GCS)
    results_list = summary.get("results", [])

    local_pub = LocalResultPublisher(store.base_uri)
    local_pub.publish(summary, results_list)

    gcs_block = config.get("gcs")
    if gcs_block:
        try:
            gcs_cfg = GCSPublisherConfig.from_dict(gcs_block)
            gcs_pub = GCSResultPublisher(gcs_cfg)
            gcs_pub.publish(summary, results_list)
        except Exception:
            log.exception("Failed to publish to GCS (non-fatal)")

    log.info("Run completed: %s", summary.get("counts", {}))


def run_job(
    config_uri: str, out_dir_uri: str, cli_execute: Optional[bool] = None
) -> None:
    config = _load_config(config_uri)
    store = ArtifactStore(base_uri=out_dir_uri)
    store.ensure_dir()

    execute_cfg = bool(config.get("execute", False))
    execute = execute_cfg if cli_execute is None else bool(cli_execute)

    # Route to BIP-driven flow when config has 'books' instead of 'requests'.
    if config.get("books") and not config.get("requests"):
        store.ensure_dir("audit")
        _run_bip_flow(config, store, execute)
        return

    store.ensure_dir("audit")
    store.ensure_dir("exceptions")

    oracle_cfg = OracleConfig.from_dict(config.get("oracle", {}))
    client = OracleErpIntegrationsClient(oracle_cfg)

    requests = config.get("requests")
    if not isinstance(requests, list) or not requests:
        raise ConfigError(
            "Config must include non-empty 'requests' or 'books' list."
        )

    results: List[Dict[str, Any]] = []
    summary = {
        "total": 0,
        "success": 0,
        "failed": 0,
        "noop": 0,
        "dry_run": (not execute),
        "started_ts": int(time.time()),
    }

    for req in requests:
        summary["total"] += 1
        try:
            _validate_request(req)
            r = _process_one(client, store, req, execute)
            results.append(r)
            if r["status"] == "SUCCESS":
                summary["success"] += 1
            elif r["status"] == "NOOP":
                summary["noop"] += 1
            else:
                summary["failed"] += 1
        except Exception as e:
            log.exception("Unhandled exception processing request")
            rid = (
                req.get("request_id", "UNKNOWN") if isinstance(req, dict) else "UNKNOWN"
            )
            err = {"request_id": rid, "status": "FAILED", "error": str(e)}
            results.append(err)
            summary["failed"] += 1
            store.write_json(
                f"exceptions/{rid}.json", {"request": req, "error": str(e)}
            )

    summary["finished_ts"] = int(time.time())
    store.write_json("summary.json", summary)
    store.write_json("results.json", results)
    log.info("Run completed: %s", summary)


def _process_one(
    client: OracleErpIntegrationsClient,
    store: ArtifactStore,
    req: Dict[str, Any],
    execute: bool,
) -> Dict[str, Any]:
    request_id = str(req["request_id"])
    transfer_type = str(req.get("transfer_type", "SAME_BOOK")).upper()
    effective_date = req.get("effective_date")
    src = req["source"]
    src_book = str(src["book_type_code"])
    src_asset_number = str(src["asset_number"])

    # Always pre-read source asset state.
    raw_get, pl_get, state = get_asset_information(client, src_book, src_asset_number)
    store.write_json(
        f"audit/{request_id}.getAssetInformation.request.json",
        {"P_BOOK_TYPE_CODE": src_book, "P_ASSET_NUMBER": src_asset_number},
    )
    store.write_json(f"audit/{request_id}.getAssetInformation.response.json", raw_get)

    context = {
        "request": req,
        "source": {
            "book_type_code": src_book,
            "asset_number": src_asset_number,
            "asset_id": state.asset_id,
        },
        "asset": {
            "asset_id": state.asset_id,
            "asset_number": state.asset_number,
            "book_type_code": state.book_type_code,
            "category_id": state.category_id,
            "date_placed_in_service": state.date_placed_in_service,
            "cost": state.cost,
            "description": state.description,
            "tag_number": state.tag_number,
        },
    }

    if transfer_type == "SAME_BOOK":
        overrides = req.get("target_assignment") or {}
        target_company = overrides.get("target_company")

        if target_company:
            # Option B: derive expense CCID by swapping the Company segment.
            company_segment_key = str(overrides.get("company_segment_key", "Segment1"))
            log.info(
                "[%s] Option B: target_company=%s, company_segment_key=%s",
                request_id,
                target_company,
                company_segment_key,
            )
            params, is_noop = build_same_book_transfer_params_option_b(
                client=client,
                state=state,
                effective_date=effective_date,
                target_company=str(target_company),
                request_id=request_id,
                company_segment_key=company_segment_key,
            )
        else:
            params, is_noop = build_same_book_transfer_params(
                state=state,
                effective_date=effective_date,
                overrides=overrides,
                request_id=request_id,
            )

        if is_noop:
            return {
                "request_id": request_id,
                "transfer_type": transfer_type,
                "status": "NOOP",
                "message": "Target assignment matches current assignment; no transfer posted.",
                "asset_id": state.asset_id,
                "asset_number": state.asset_number,
                "book_type_code": state.book_type_code,
            }

        store.write_json(
            f"audit/{request_id}.transferAsset.request.json",
            {
                "OperationName": "processTransaction-transferAsset",
                "ParameterList_params": params,
            },
        )
        if not execute:
            return {
                "request_id": request_id,
                "transfer_type": transfer_type,
                "status": "SUCCESS",
                "dry_run": True,
                "message": "Dry-run: transferAsset payload built; no POST executed.",
                "asset_id": state.asset_id,
                "asset_number": state.asset_number,
                "book_type_code": state.book_type_code,
                "planned_handle": "transferAsset",
            }

        raw_txn, pl_txn = client.process_transaction("transferAsset", params)
        store.write_json(f"audit/{request_id}.transferAsset.response.json", raw_txn)

        return _result_from_fusion_response(
            request_id, transfer_type, state, "transferAsset", pl_txn
        )

    # XBOOK
    xbook = req.get("xbook") if isinstance(req.get("xbook"), dict) else None
    strategy = xbook.get("strategy", "").lower() if xbook else ""

    # Strategy 1: built-in bookTransfer builder (produces all Oracle-expected params)
    if strategy in ("native", "builtin"):
        target = req.get("target")
        if not isinstance(target, dict) or not target.get("book_type_code"):
            raise ConfigError("xbook native/builtin requires target.book_type_code.")
        dest_book = str(target["book_type_code"])
        overrides = req.get("target_assignment") or {}
        # Merge xbook-level overrides (flags, book_transfer_type_code, etc.)
        if xbook:
            for xk, xv in xbook.items():
                if xk not in ("strategy", "handle", "parameters_template"):
                    overrides.setdefault(xk, xv)

        handle = (
            str(xbook.get("handle") or "transferAsset").strip()
            if xbook
            else "transferAsset"
        )

        # If a parameters_template is provided, use template rendering (legacy native path).
        tmpl = xbook.get("parameters_template") if xbook else None
        if isinstance(tmpl, dict) and tmpl:
            rendered_params = render(tmpl, context)
            params = rendered_params
        else:
            params = build_book_transfer_params(
                state=state,
                dest_book_type_code=dest_book,
                effective_date=effective_date,
                overrides=overrides,
                request_id=request_id,
            )

        store.write_json(
            f"audit/{request_id}.xbook.native.request.json",
            {"handle": handle, "params": params},
        )
        if not execute:
            return {
                "request_id": request_id,
                "transfer_type": transfer_type,
                "status": "SUCCESS",
                "dry_run": True,
                "message": "Dry-run: native xBook payload built; no POST executed.",
                "asset_id": state.asset_id,
                "asset_number": state.asset_number,
                "book_type_code": state.book_type_code,
                "planned_handle": handle,
            }

        raw_txn, pl_txn = client.process_transaction(handle, params)
        store.write_json(f"audit/{request_id}.xbook.native.response.json", raw_txn)
        return _result_from_fusion_response(
            request_id, transfer_type, state, handle, pl_txn
        )

    # Strategy 2: orchestration fallback (addAsset in target book + retireAsset in source book)
    target = req.get("target")
    if not isinstance(target, dict) or not target.get("book_type_code"):
        raise ConfigError("XBOOK orchestration requires target.book_type_code.")
    tgt_book = str(target["book_type_code"])

    tgt_asset_number = str(target.get("asset_number") or "").strip()
    if not tgt_asset_number:
        # Deterministic generator (can be replaced by upstream service).
        tgt_asset_number = f"XFER_{state.asset_number}_{int(time.time())}"

    overrides = req.get("target_assignment") or {}

    add_params = build_add_asset_params(
        state=state,
        target_book_type_code=tgt_book,
        target_asset_number=tgt_asset_number,
        effective_date=effective_date,
        overrides=overrides,
        request_id=request_id,
    )
    store.write_json(
        f"audit/{request_id}.addAsset.request.json",
        {
            "OperationName": "processTransaction-addAsset",
            "ParameterList_params": add_params,
        },
    )

    retire_params = build_retire_asset_params(
        state=state, effective_date=effective_date, request_id=request_id
    )
    store.write_json(
        f"audit/{request_id}.retireAsset.request.json",
        {
            "OperationName": "processTransaction-retireAsset",
            "ParameterList_params": retire_params,
        },
    )

    if not execute:
        return {
            "request_id": request_id,
            "transfer_type": transfer_type,
            "status": "SUCCESS",
            "dry_run": True,
            "message": "Dry-run: xBook orchestration payloads built (addAsset + retireAsset); no POST executed.",
            "source_asset_id": state.asset_id,
            "source_asset_number": state.asset_number,
            "source_book_type_code": state.book_type_code,
            "target_asset_number": tgt_asset_number,
            "target_book_type_code": tgt_book,
            "planned_handles": ["addAsset", "retireAsset"],
        }

    raw_add, pl_add = client.process_transaction("addAsset", add_params)
    store.write_json(f"audit/{request_id}.addAsset.response.json", raw_add)

    # If addAsset fails, do not retire source.
    add_status = str(pl_add.get("X_RETURN_STATUS") or "").strip()
    if add_status and add_status != "S":
        raise FusionApiError(
            f"addAsset failed; not retiring source. Response: {pl_add}"
        )

    raw_ret, pl_ret = client.process_transaction("retireAsset", retire_params)
    store.write_json(f"audit/{request_id}.retireAsset.response.json", raw_ret)

    # Build consolidated result
    return {
        "request_id": request_id,
        "transfer_type": transfer_type,
        "status": "SUCCESS"
        if str(pl_ret.get("X_RETURN_STATUS") or "") == "S"
        else "FAILED",
        "mode": "orchestrated_add_retire",
        "source_asset_id": state.asset_id,
        "source_asset_number": state.asset_number,
        "source_book_type_code": state.book_type_code,
        "target_asset_number": pl_add.get("PX_ASSET_NUMBER") or tgt_asset_number,
        "target_book_type_code": tgt_book,
        "addAsset": _compact_pl(pl_add),
        "retireAsset": _compact_pl(pl_ret),
    }


def _compact_pl(pl: Dict[str, Any]) -> Dict[str, Any]:
    keys = [
        "X_RETURN_STATUS",
        "X_EVENT_ID",
        "X_TRANSACTION_HEADER_ID",
        "X_D_TRANSACTION_HEADER_ID",
        "X_RETIREMENT_ID",
        "X_MSG_COUNT",
        "X_MSG_DATA",
        "PX_ASSET_NUMBER",
    ]
    return {k: pl.get(k) for k in keys if k in pl}


def _result_from_fusion_response(
    request_id: str,
    transfer_type: str,
    state: Any,
    handle: str,
    pl: Dict[str, Any],
) -> Dict[str, Any]:
    status = str(pl.get("X_RETURN_STATUS") or "").strip()
    out = {
        "request_id": request_id,
        "transfer_type": transfer_type,
        "handle": handle,
        "asset_id": getattr(state, "asset_id", None),
        "asset_number": getattr(state, "asset_number", None),
        "book_type_code": getattr(state, "book_type_code", None),
        "fusion": _compact_pl(pl),
    }
    if status == "S":
        out["status"] = "SUCCESS"
        return out

    # Per Oracle guidance, X_RETURN_STATUS can be F even when X_EVENT_ID exists;
    # treat as failure but preserve identifiers for investigation.
    out["status"] = "FAILED"
    out["message"] = (
        "Fusion returned non-success X_RETURN_STATUS; see fusion payload for details."
    )
    return out
# <<< END: job.py <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<