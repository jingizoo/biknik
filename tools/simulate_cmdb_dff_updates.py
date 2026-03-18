#!/usr/bin/env python3
"""Create test data and simulate CMDB-driven DFF updates for OFAM.

What this does
---------------
1. Loads a scenario catalogue (JSON) describing source assets + CMDB DFF deltas.
2. Converts those scenarios into BIP-style rows (the interface the current code consumes).
3. Runs the existing ``FusionIUSync`` discovery/execution flow against mock BIP + mock
   Oracle clients.
4. Writes ready-to-review artifacts:
   - all_scenarios.json
   - selected_scenarios.json
   - collapsed_scenarios.json
   - bip_rows_all.json / bip_rows_selected.json
   - bip_rows_all.csv / bip_rows_selected.csv
   - pending_transfers.json
   - summary.json
   - results.json
   - scenario_matrix.csv
   - gap_report.md
   - mock_api_log.json
   - daily NDJSON logs via LocalResultPublisher

Why this is the right test harness
----------------------------------
The current program does not consume raw CMDB events directly. It consumes DFF values
that are surfaced through the BI Publisher report. So the most practical way to
simulate a CMDB update is to generate the exact BIP row shape that the code already
expects, then let the existing transfer engine process those rows.

Examples
--------
  python tools/simulate_cmdb_dff_updates.py
  python tools/simulate_cmdb_dff_updates.py --policy none
  python tools/simulate_cmdb_dff_updates.py --run-mode dry-run --out-dir ./sim_output
  python tools/simulate_cmdb_dff_updates.py --scenario-file tools/cmdb_dff_scenarios.json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "main" / "python"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from ofam_asset_xfer.entity_resolver import EntityBookResolver
from ofam_asset_xfer.exceptions import FusionApiError
from ofam_asset_xfer.fusion_ops import AssetState
from ofam_asset_xfer.fusion_sync import FusionIUSync, PendingTransfer
from ofam_asset_xfer.local_publisher import LocalResultPublisher


DEFAULT_ENTITY_BOOK_MAP: Dict[str, str] = {
    "US Entity": "US CORP BOOK",
    "UK Entity": "UK CORP BOOK",
    "JP Entity": "JP CORP BOOK",
    "DE Entity": "DE CORP BOOK",
    "IN Entity": "IN CORP BOOK",
}

DEFAULT_SCENARIO_FILE = Path(__file__).with_name("cmdb_dff_scenarios.json")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def _write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: List[str] = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _scalarize(v) for k, v in row.items()})


def _scalarize(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True)
    return value


def _scenario_source(s: Dict[str, Any]) -> Dict[str, Any]:
    return dict(s.get("source", {}))


def _scenario_dff(s: Dict[str, Any]) -> Dict[str, Any]:
    return dict(s.get("dff_update", {}))


def _scenario_sim(s: Dict[str, Any]) -> Dict[str, Any]:
    return dict(s.get("simulate", {}))


def _scenario_expected(s: Dict[str, Any]) -> Dict[str, Any]:
    return dict(s.get("expected", {}))


def _resolved_target_book(s: Dict[str, Any], entity_map: Dict[str, str]) -> str:
    dff = _scenario_dff(s)
    final_book = str(dff.get("final_target_book_type_code") or "").strip()
    if final_book:
        return final_book
    entity = str(dff.get("transfer_to_entity") or "").strip()
    return entity_map.get(entity, "")


def _scenario_key(s: Dict[str, Any], entity_map: Dict[str, str]) -> Tuple[str, str, str, str, str]:
    src = _scenario_source(s)
    dff = _scenario_dff(s)
    return (
        str(src.get("asset_id", "")).strip(),
        str(dff.get("transfer_date", "")).strip(),
        _resolved_target_book(s, entity_map),
        str(dff.get("target_location_id") or dff.get("transfer_to_location") or "").strip(),
        str(dff.get("target_expense_ccid") or "").strip(),
    )


def _pending_key(p: PendingTransfer) -> Tuple[str, str, str, str, str]:
    return (
        str(p.asset_id or "").strip(),
        str(p.transfer_date or "").strip(),
        str(p.target_book_type_code or "").strip(),
        str(p.target_location_id or p.transfer_to_location or "").strip(),
        str(p.target_expense_ccid or "").strip(),
    )


def _transfer_key_from_params(params: Dict[str, Any]) -> Tuple[str, str, str, str, str]:
    asset_id = str(params.get("P_ASSET_ID") or "").strip()
    tx_date = str(params.get("P_TRANSACTION_DATE_ENTERED") or "").strip()
    target_book = str(params.get("P_DEST_BOOK_TYPE_CODE") or params.get("P_BOOK_TYPE_CODE") or "").strip()

    loc_tbl = [str(x) for x in (params.get("P_LOCATION_CCID_TBL") or [])]
    exp_tbl = [str(x) for x in (params.get("P_EXPENSE_CCID_TBL") or [])]

    target_location = ""
    target_expense = ""
    if len(loc_tbl) >= 2:
        target_location = loc_tbl[1]
    elif loc_tbl:
        target_location = loc_tbl[0]

    if len(exp_tbl) >= 2:
        target_expense = exp_tbl[1]
    elif exp_tbl:
        target_expense = exp_tbl[0]

    return (asset_id, tx_date, target_book, target_location, target_expense)


def _build_asset_state(src: Dict[str, Any]) -> AssetState:
    return AssetState(
        asset_id=str(src["asset_id"]),
        asset_number=str(src["asset_number"]),
        book_type_code=str(src["book_type_code"]),
        distribution_ids=[str(x) for x in src["distribution_ids"]],
        units_assigned=[str(x) for x in src["units_assigned"]],
        assigned_to=[str(x) for x in src.get("assigned_to", [""] * len(src["distribution_ids"]))],
        expense_ccids=[str(x) for x in src["expense_ccids"]],
        location_ccids=[str(x) for x in src["location_ccids"]],
        category_id=str(src.get("category_id") or "").strip() or None,
        date_placed_in_service=str(src.get("date_placed_in_service") or "").strip() or None,
        cost=str(src.get("cost") or "").strip() or None,
        description=str(src.get("description") or "").strip() or None,
        tag_number=str(src.get("tag_number") or "").strip() or None,
    )


def _build_get_asset_info_response(src: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    assigned_to = src.get("assigned_to", [""] * len(src["distribution_ids"]))
    pl = {
        "X_RETURN_STATUS": "S",
        "X_ASSET_ID": str(src["asset_id"]),
        "X_ASSET_NUMBER": str(src["asset_number"]),
        "X_BOOK_TYPE_CODE": str(src["book_type_code"]),
        "X_DISTRIBUTION_ID_TBL": ",".join(str(x) for x in src["distribution_ids"]),
        "X_UNITS_ASSIGNED_TBL": ",".join(str(x) for x in src["units_assigned"]),
        "X_ASSIGNED_TO_TBL": ",".join(str(x) for x in assigned_to),
        "X_EXPENSE_CCID_TBL": ",".join(str(x) for x in src["expense_ccids"]),
        "X_LOCATION_CCID_TBL": ",".join(str(x) for x in src["location_ccids"]),
        "X_CATEGORY_ID": str(src.get("category_id") or ""),
        "X_DATE_PLACED_IN_SERVICE": str(src.get("date_placed_in_service") or ""),
        "X_COST": str(src.get("cost") or ""),
        "X_DESCRIPTION": str(src.get("description") or ""),
        "X_TAG_NUMBER": str(src.get("tag_number") or ""),
    }
    return ({}, pl)


def _build_bip_row(s: Dict[str, Any]) -> Dict[str, str]:
    src = _scenario_source(s)
    dff = _scenario_dff(s)
    row = {
        "CASE_ID": str(s.get("case_id", "")),
        "ASSET_ID": str(src.get("asset_id", "")),
        "ASSET_NUMBER": str(src.get("asset_number", "")),
        "P_BOOK_TYPE_CODE": str(src.get("book_type_code", "")),
        "TRANSFER_TO_ENTITY": str(dff.get("transfer_to_entity", "")),
        "TRANSFER_DATE": str(dff.get("transfer_date", "")),
        "TRANSFER_TO_LOCATION": str(dff.get("transfer_to_location", "")),
        "FINAL_TARGET_BOOK_TYPE_CODE": str(dff.get("final_target_book_type_code", "")),
        "TARGET_LOCATION_ID": str(dff.get("target_location_id", "")),
        "CURRENT_LOCATION_ID": str(
            dff.get("current_location_id")
            or (src.get("location_ccids") or [""])[0]
        ),
        "TARGET_EXPENSE_CCID": str(dff.get("target_expense_ccid", "")),
    }
    extra = dff.get("extra_columns") or {}
    for key, value in extra.items():
        row[str(key)] = str(value)
    return row


def _all_books(scenarios: Iterable[Dict[str, Any]], entity_map: Dict[str, str]) -> List[str]:
    books = set(entity_map.values())
    for s in scenarios:
        src = _scenario_source(s)
        if src.get("book_type_code"):
            books.add(str(src["book_type_code"]))
        target = _resolved_target_book(s, entity_map)
        if target:
            books.add(target)
    return sorted(books)


def _load_scenarios(path: Path) -> List[Dict[str, Any]]:
    raw = _read_json(path)
    if not isinstance(raw, list):
        raise ValueError(f"Scenario file must contain a JSON array: {path}")
    scenarios: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Each scenario must be a JSON object.")
        src = _scenario_source(item)
        dff = _scenario_dff(item)
        if not src.get("asset_id") or not src.get("asset_number") or not src.get("book_type_code"):
            raise ValueError(f"Scenario missing required source identity fields: {item}")
        if not dff.get("transfer_date") or not dff.get("transfer_to_entity"):
            raise ValueError(f"Scenario missing required dff_update fields: {item}")
        scenarios.append(item)
    return scenarios


def _apply_final_state_wins(
    scenarios: Sequence[Dict[str, Any]],
    entity_map: Dict[str, str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for s in scenarios:
        src = _scenario_source(s)
        group = str(s.get("event_group") or src.get("asset_number") or s.get("case_id"))
        grouped[group].append(s)

    selected: List[Dict[str, Any]] = []
    collapsed: List[Dict[str, Any]] = []
    for _group, items in grouped.items():
        items_sorted = sorted(
            items,
            key=lambda x: (
                int(x.get("sequence", 0)),
                str(_scenario_dff(x).get("transfer_date") or ""),
                str(x.get("case_id") or ""),
            ),
        )
        winner = items_sorted[-1]
        selected.append(winner)
        collapsed.extend(items_sorted[:-1])

    selected_sorted = sorted(
        selected,
        key=lambda x: (
            str(_scenario_source(x).get("book_type_code") or ""),
            str(_scenario_source(x).get("asset_number") or ""),
            str(_scenario_dff(x).get("transfer_date") or ""),
            str(_scenario_dff(x).get("target_location_id") or _scenario_dff(x).get("transfer_to_location") or ""),
            _resolved_target_book(x, entity_map),
        ),
    )
    collapsed_sorted = sorted(collapsed, key=lambda x: str(x.get("case_id") or ""))
    return selected_sorted, collapsed_sorted


class MockBIPClient:
    def __init__(self, rows: Sequence[Dict[str, str]]):
        self._rows_by_book: Dict[str, List[Dict[str, str]]] = defaultdict(list)
        for row in rows:
            self._rows_by_book[str(row.get("P_BOOK_TYPE_CODE", "")).strip()].append(dict(row))

    def run_report(self, params: Optional[Dict[str, str]] = None) -> List[Dict[str, str]]:
        params = params or {}
        book = str(params.get("P_BOOK_TYPE_CODE") or "").strip()
        return [dict(r) for r in self._rows_by_book.get(book, [])]


class MockFusionClient:
    def __init__(
        self,
        selected_scenarios: Sequence[Dict[str, Any]],
        entity_map: Dict[str, str],
    ) -> None:
        self._entity_map = entity_map
        self.call_log: List[Dict[str, Any]] = []
        self._asset_by_book_number: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._asset_by_id: Dict[str, Dict[str, Any]] = {}
        self._transfer_rules_by_key: Dict[Tuple[str, str, str, str, str], Dict[str, Any]] = {}
        self._transfer_rules_by_asset_date_book: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
        self._period_transfers: Dict[Tuple[str, str], int] = defaultdict(int)
        self._next_event = 90000

        for scenario in selected_scenarios:
            src = _scenario_source(scenario)
            asset_key = (str(src.get("book_type_code")), str(src.get("asset_number")))
            self._asset_by_book_number.setdefault(asset_key, scenario)
            self._asset_by_id.setdefault(str(src.get("asset_id")), scenario)

            key = _scenario_key(scenario, entity_map)
            self._transfer_rules_by_key[key] = scenario
            self._transfer_rules_by_asset_date_book[(key[0], key[1], key[2])].append(scenario)

    def process_transaction(self, handle: str, params: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        self.call_log.append(
            {
                "ts": int(time.time()),
                "handle": handle,
                "params": json.loads(json.dumps(params, default=str)),
            }
        )

        if handle == "getAssetInformation":
            return self._mock_get_asset_information(params)
        if handle in {"bookTransfer", "transferAsset"}:
            return self._mock_transfer(handle, params)
        raise FusionApiError(f"Unsupported mock handle: {handle}")

    def _mock_get_asset_information(self, params: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        book = str(params.get("P_BOOK_TYPE_CODE") or "").strip()
        asset_number = str(params.get("P_ASSET_NUMBER") or "").strip()
        scenario = self._asset_by_book_number.get((book, asset_number))
        if not scenario:
            raise FusionApiError(f"Mock asset not found for {book}/{asset_number}")

        sim = _scenario_sim(scenario)
        if sim.get("get_asset_information_error"):
            raise FusionApiError(str(sim["get_asset_information_error"]))

        raw, pl = _build_get_asset_info_response(_scenario_source(scenario))
        self.call_log[-1]["matched_case_id"] = str(scenario.get("case_id"))
        return raw, pl

    def _mock_transfer(self, handle: str, params: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        key = _transfer_key_from_params(params)
        scenario = self._transfer_rules_by_key.get(key)

        if not scenario:
            asset_id, tx_date, target_book, _loc, _exp = key
            fallback_candidates = self._transfer_rules_by_asset_date_book.get((asset_id, tx_date, target_book), [])
            scenario = fallback_candidates[0] if fallback_candidates else None

        if not scenario:
            raise FusionApiError(f"No mock transfer rule matches params key={key}")

        sim = _scenario_sim(scenario)
        src = _scenario_source(scenario)
        self.call_log[-1]["matched_case_id"] = str(scenario.get("case_id"))

        asset_id = str(src.get("asset_id"))
        tx_date = str(_scenario_dff(scenario).get("transfer_date") or "")
        period = tx_date[:7]

        if sim.get("same_period_retransfer_block") and self._period_transfers[(asset_id, period)] > 0:
            status = "E"
            message = str(sim.get("error_message") or "Oracle API blocks same-period re-transfer for this asset")
            return ({}, {"X_RETURN_STATUS": status, "X_MESSAGE_TEXT": message})

        if sim.get("raise_error"):
            raise FusionApiError(str(sim["raise_error"]))

        status = str(sim.get("transfer_status") or "S").strip().upper()
        if status == "S":
            self._period_transfers[(asset_id, period)] += 1
            self._next_event += 1
            target_book = str(_resolved_target_book(scenario, self._entity_map) or src.get("book_type_code") or "")
            is_cross_book = target_book.strip().upper() != str(src.get("book_type_code") or "").strip().upper()
            response = {
                "X_RETURN_STATUS": "S",
                "X_EVENT_ID": str(self._next_event),
                "X_TARGET_BOOK_TYPE_CODE": target_book,
            }
            if is_cross_book:
                response["X_TARGET_ASSET_NUMBER"] = str(
                    sim.get("mock_target_asset_number") or f"{src.get('asset_number')}_XFER"
                )
                response["X_TARGET_ASSET_ID"] = str(int(str(src.get("asset_id"))) + 100000)
            return ({}, response)

        return ({}, {"X_RETURN_STATUS": status, "X_MESSAGE_TEXT": str(sim.get("error_message") or "Simulated failure")})


def _result_to_dict(result: Any) -> Dict[str, Any]:
    return {
        "asset_number": result.asset_number,
        "status": result.status,
        "source_book": result.source_book,
        "target_book": result.target_book,
        "transfer_to_entity": result.transfer_to_entity,
        "transfer_date": result.transfer_date,
        "transfer_classification": result.transfer_classification,
        "error": result.error,
        "fusion_response": result.fusion_response,
    }


def _build_summary(
    books: Sequence[str],
    dry_run: bool,
    results: Sequence[Dict[str, Any]],
    policy: str,
    selected_count: int,
    collapsed_count: int,
) -> Dict[str, Any]:
    counts = {
        "total_selected": selected_count,
        "collapsed_by_policy": collapsed_count,
        "executed_or_planned": len(results),
        "transferred": 0,
        "failed": 0,
        "dry_run": 0,
        "noop": 0,
    }
    for row in results:
        status = str(row.get("status") or "").upper()
        if status == "TRANSFERRED":
            counts["transferred"] += 1
        elif status == "FAILED":
            counts["failed"] += 1
        elif status == "DRY_RUN":
            counts["dry_run"] += 1
        elif status == "NOOP":
            counts["noop"] += 1

    return {
        "run_ts": int(time.time()),
        "run_date": date.today().isoformat(),
        "dry_run": dry_run,
        "policy": policy,
        "books": list(books),
        "counts": counts,
        "results": list(results),
    }


def _flatten_scenario(s: Dict[str, Any], entity_map: Dict[str, str]) -> Dict[str, Any]:
    src = _scenario_source(s)
    dff = _scenario_dff(s)
    expected = _scenario_expected(s)
    return {
        "case_id": s.get("case_id"),
        "sequence": s.get("sequence"),
        "event_group": s.get("event_group", src.get("asset_number")),
        "asset_id": src.get("asset_id"),
        "asset_number": src.get("asset_number"),
        "source_book_type_code": src.get("book_type_code"),
        "current_expense_ccids": src.get("expense_ccids"),
        "current_location_ccids": src.get("location_ccids"),
        "transfer_to_entity": dff.get("transfer_to_entity"),
        "transfer_date": dff.get("transfer_date"),
        "transfer_to_location": dff.get("transfer_to_location"),
        "final_target_book_type_code": dff.get("final_target_book_type_code"),
        "resolved_target_book_type_code": _resolved_target_book(s, entity_map),
        "target_location_id": dff.get("target_location_id"),
        "target_expense_ccid": dff.get("target_expense_ccid"),
        "extra_columns": dff.get("extra_columns", {}),
        "expected_business_status": expected.get("business_status"),
        "expected_notes": expected.get("notes"),
        "description": s.get("description"),
    }


def _write_gap_report(
    path: Path,
    policy: str,
    run_mode: str,
    summary: Dict[str, Any],
    matrix_rows: Sequence[Dict[str, Any]],
) -> None:
    lines = [
        "# CMDB DFF Simulation Gap Report",
        "",
        f"- Policy: `{policy}`",
        f"- Run mode: `{run_mode}`",
        f"- Run date: `{summary.get('run_date')}`",
        f"- Selected scenarios: `{summary.get('counts', {}).get('total_selected')}`",
        f"- Collapsed by policy: `{summary.get('counts', {}).get('collapsed_by_policy')}`",
        f"- Transferred: `{summary.get('counts', {}).get('transferred')}`",
        f"- Failed: `{summary.get('counts', {}).get('failed')}`",
        f"- Dry-run: `{summary.get('counts', {}).get('dry_run')}`",
        f"- Noop: `{summary.get('counts', {}).get('noop')}`",
        "",
        "## Case-by-case",
        "",
        "| Case | Expected | Actual | Gap | Notes |",
        "|---|---|---|---|---|",
    ]
    for row in matrix_rows:
        lines.append(
            "| {case_id} | {expected_business_status} | {actual_status} | {gap_flag} | {notes} |".format(
                case_id=row.get("case_id", ""),
                expected_business_status=row.get("expected_business_status", ""),
                actual_status=row.get("actual_status", ""),
                gap_flag="YES" if row.get("gap") else "NO",
                notes=str(row.get("notes", "")).replace("|", "/"),
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_scenario_matrix(
    all_scenarios: Sequence[Dict[str, Any]],
    selected_scenarios: Sequence[Dict[str, Any]],
    collapsed_scenarios: Sequence[Dict[str, Any]],
    entity_map: Dict[str, str],
    pending: Sequence[PendingTransfer],
    results_by_key: Dict[Tuple[str, str, str, str, str], Dict[str, Any]],
    run_mode: str,
) -> List[Dict[str, Any]]:
    selected_case_ids = {str(s.get("case_id")) for s in selected_scenarios}
    collapsed_case_ids = {str(s.get("case_id")) for s in collapsed_scenarios}
    discovered_keys = {_pending_key(p) for p in pending}

    rows: List[Dict[str, Any]] = []
    for scenario in all_scenarios:
        case_id = str(scenario.get("case_id"))
        expected_status = str(_scenario_expected(scenario).get("business_status") or "")
        key = _scenario_key(scenario, entity_map)

        if case_id in collapsed_case_ids:
            actual_status = "COLLAPSED_BY_POLICY"
            actual_notes = "Suppressed by final-state-wins dedupe before discovery."
        elif key not in discovered_keys:
            actual_status = "SKIPPED_DISCOVERY"
            actual_notes = "Did not survive discovery filters."
        else:
            result = results_by_key.get(key)
            if result is None:
                actual_status = "DISCOVERED_NOT_EXECUTED"
                actual_notes = "Discovered but no execution result recorded."
            else:
                actual_status = str(result.get("status") or "")
                actual_notes = str(result.get("error") or result.get("fusion_response") or "")

        gap = actual_status != expected_status
        if run_mode == "dry-run" and expected_status == "TRANSFERRED" and actual_status == "DRY_RUN":
            gap = False
        if run_mode == "dry-run" and expected_status == "FAILED" and actual_status == "DRY_RUN":
            gap = False

        rows.append(
            {
                "case_id": case_id,
                "sequence": scenario.get("sequence"),
                "asset_number": _scenario_source(scenario).get("asset_number"),
                "source_book_type_code": _scenario_source(scenario).get("book_type_code"),
                "resolved_target_book_type_code": _resolved_target_book(scenario, entity_map),
                "transfer_date": _scenario_dff(scenario).get("transfer_date"),
                "expected_business_status": expected_status,
                "actual_status": actual_status,
                "gap": gap,
                "notes": str(_scenario_expected(scenario).get("notes") or "") + (
                    f" Actual: {actual_notes}" if actual_notes else ""
                ),
                "selected_for_run": case_id in selected_case_ids,
            }
        )
    return rows


def run_simulation(
    scenario_file: Path,
    out_dir: Path,
    policy: str,
    run_mode: str,
    entity_map: Dict[str, str],
) -> Dict[str, Any]:
    scenarios = _load_scenarios(scenario_file)
    selected_scenarios = list(scenarios)
    collapsed_scenarios: List[Dict[str, Any]] = []
    if policy == "final-state-wins":
        selected_scenarios, collapsed_scenarios = _apply_final_state_wins(scenarios, entity_map)
    elif policy != "none":
        raise ValueError(f"Unsupported policy: {policy}")

    all_books = _all_books(selected_scenarios, entity_map)
    bip_rows_all = [_build_bip_row(s) for s in scenarios]
    bip_rows_selected = [_build_bip_row(s) for s in selected_scenarios]

    bip_client = MockBIPClient(bip_rows_selected)
    fusion_client = MockFusionClient(selected_scenarios, entity_map)
    sync = FusionIUSync(fusion_client, EntityBookResolver(entity_map), bip_client)

    pending = sync.find_pending_transfers(books=all_books, limit=len(selected_scenarios) + 20)

    dry_run = run_mode == "dry-run"
    results: List[Dict[str, Any]] = []
    results_by_key: Dict[Tuple[str, str, str, str, str], Dict[str, Any]] = {}
    for p in pending:
        result = sync.execute_transfer(p, dry_run=dry_run)
        result_dict = _result_to_dict(result)
        results.append(result_dict)
        results_by_key[_pending_key(p)] = result_dict

    summary = _build_summary(
        books=all_books,
        dry_run=dry_run,
        results=results,
        policy=policy,
        selected_count=len(selected_scenarios),
        collapsed_count=len(collapsed_scenarios),
    )

    matrix_rows = _build_scenario_matrix(
        all_scenarios=scenarios,
        selected_scenarios=selected_scenarios,
        collapsed_scenarios=collapsed_scenarios,
        entity_map=entity_map,
        pending=pending,
        results_by_key=results_by_key,
        run_mode=run_mode,
    )

    local_pub = LocalResultPublisher(str(out_dir))
    local_pub.publish(summary, results)

    _write_json(out_dir / "all_scenarios.json", scenarios)
    _write_json(out_dir / "selected_scenarios.json", selected_scenarios)
    _write_json(out_dir / "collapsed_scenarios.json", collapsed_scenarios)
    _write_json(out_dir / "bip_rows_all.json", bip_rows_all)
    _write_json(out_dir / "bip_rows_selected.json", bip_rows_selected)
    _write_json(out_dir / "pending_transfers.json", [p.__dict__ for p in pending])
    _write_json(out_dir / "summary.json", summary)
    _write_json(out_dir / "results.json", results)
    _write_json(out_dir / "mock_api_log.json", fusion_client.call_log)
    _write_csv(out_dir / "bip_rows_all.csv", bip_rows_all)
    _write_csv(out_dir / "bip_rows_selected.csv", bip_rows_selected)
    _write_csv(out_dir / "scenario_matrix.csv", matrix_rows)
    _write_gap_report(out_dir / "gap_report.md", policy, run_mode, summary, matrix_rows)

    flat_cases = [_flatten_scenario(s, entity_map) for s in scenarios]
    _write_csv(out_dir / "scenario_catalog.csv", flat_cases)

    return {
        "summary": summary,
        "matrix_rows": matrix_rows,
        "out_dir": str(out_dir),
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate CMDB DFF updates for OFAM using mock BIP + mock Oracle.")
    parser.add_argument(
        "--scenario-file",
        default=str(DEFAULT_SCENARIO_FILE),
        help="Path to scenario JSON file.",
    )
    parser.add_argument(
        "--out-dir",
        default=str(REPO_ROOT / "output" / "cmdb_dff_simulation"),
        help="Directory for generated artifacts.",
    )
    parser.add_argument(
        "--policy",
        choices=["final-state-wins", "none"],
        default="final-state-wins",
        help="How to handle multiple queued CMDB events for the same asset.",
    )
    parser.add_argument(
        "--run-mode",
        choices=["execute", "dry-run"],
        default="execute",
        help="Execute simulated transfers or only build payloads.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    result = run_simulation(
        scenario_file=Path(args.scenario_file),
        out_dir=Path(args.out_dir),
        policy=args.policy,
        run_mode=args.run_mode,
        entity_map=DEFAULT_ENTITY_BOOK_MAP,
    )
    summary = result["summary"]
    counts = summary["counts"]
    print(f"Out dir               : {result['out_dir']}")
    print(f"Policy                : {summary['policy']}")
    print(f"Run mode              : {'dry-run' if summary['dry_run'] else 'execute'}")
    print(f"Selected scenarios    : {counts['total_selected']}")
    print(f"Collapsed by policy   : {counts['collapsed_by_policy']}")
    print(f"Executed / planned    : {counts['executed_or_planned']}")
    print(f"Transferred           : {counts['transferred']}")
    print(f"Failed                : {counts['failed']}")
    print(f"Dry-run               : {counts['dry_run']}")
    print(f"Noop                  : {counts['noop']}")

    gaps = [r for r in result["matrix_rows"] if r.get("gap")]
    print(f"Gap rows              : {len(gaps)}")
    if gaps:
        print("Top gaps:")
        for row in gaps[:5]:
            print(f"  - {row['case_id']}: expected {row['expected_business_status']} but got {row['actual_status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
