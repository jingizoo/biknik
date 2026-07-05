"""CSV import dry-run validator (#92).

Step 1 of the Pilot Onboarding Import Wizard: an operator uploads
spreadsheet-shaped rows for teams, players, officials, rinks, and ice slots
and gets back a structured validation report — WITHOUT writing anything to
the database. Commit/write flows are separate follow-up slices (#93-#96).

Scope decision: teams/players/officials/rinks have no "external code" concept
in the domain yet (only opaque store ids). This importer is the FIRST place
introducing spreadsheet-style codes (``team_code``, ``player_code``,
``official_code``, ``rink_code``), so a row's cross-sheet reference (a
player's ``team_code``, an ice slot's ``rink_code``) is validated only
against the OTHER SHEETS IN THE SAME UPLOAD, never against existing store
records — there is no store lookup by external code yet. Mapping an external
code to an existing (or new) store record is #93's job.

``validate_import`` is a pure function: it never receives or touches a
store, so "no database writes" holds by construction, not just by
convention.

CSV column shapes (header row + data rows; no template/UI in this slice):
    teams.csv:      team_code, team_name, club_name, division_name
    players.csv:    player_code, first_name, last_name, team_code,
                     jersey_number, position, email
    officials.csv:  official_code, name, email, home_club_name
    rinks.csv:      venue_name, rink_code, rink_name, address
    ice_slots.csv:  rink_code, start_time, end_time, slot_type
"""

import csv
import io
from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..domain import IceSlotType

IMPORT_SHEET_NAMES = ("teams", "players", "officials", "rinks", "ice_slots")

_REQUIRED_FIELDS = {
    "teams": ("team_code", "team_name"),
    "players": ("player_code", "first_name", "last_name", "team_code"),
    "officials": ("official_code", "name"),
    "rinks": ("venue_name", "rink_code"),
    "ice_slots": ("rink_code", "start_time", "end_time", "slot_type"),
}

_UNIQUE_FIELD = {
    "teams": "team_code",
    "players": "player_code",
    "officials": "official_code",
    "rinks": "rink_code",
}

_VALID_SLOT_TYPES = {t.value for t in IceSlotType}


def parse_csv_text(text: str) -> List[dict]:
    """Parse raw CSV text (header row + data rows) into a list of row dicts.

    Row numbers used elsewhere are 1-indexed against the DATA rows returned
    here — row 1 is the first row after the header, not the header itself.
    """
    if not text:
        return []
    return list(csv.DictReader(io.StringIO(text)))


def _blank(value) -> bool:
    return value is None or not str(value).strip()


def _clean(value) -> str:
    return str(value).strip()


def _parse_iso_utc(value) -> Optional[datetime]:
    """Parse a timezone-aware ISO-8601 timestamp, else None (caller reports
    the error) — mirrors the tz-aware-UTC convention used elsewhere in the
    domain (see ``SetupService._require_utc`` / ``api.service._parse_dt``)."""
    if _blank(value):
        return None
    try:
        parsed = datetime.fromisoformat(_clean(value))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


class _Report:
    def __init__(self):
        self.errors: List[dict] = []
        self.warnings: List[dict] = []

    def error(self, sheet: str, row: int, message: str, field: str = None):
        entry = {"sheet": sheet, "row": row, "message": message}
        if field is not None:
            entry["field"] = field
        self.errors.append(entry)

    def warning(self, sheet: str, row: int, message: str):
        self.warnings.append({"sheet": sheet, "row": row, "message": message})


def _check_required(report: _Report, sheet: str, rows: List[dict]) -> None:
    for i, row in enumerate(rows, start=1):
        for field in _REQUIRED_FIELDS[sheet]:
            if _blank(row.get(field)):
                report.error(sheet, i, f"{field} is required.", field=field)


def _check_unique(report: _Report, sheet: str, rows: List[dict]) -> None:
    field = _UNIQUE_FIELD[sheet]
    seen = set()
    for i, row in enumerate(rows, start=1):
        value = row.get(field)
        if _blank(value):
            continue  # already reported as missing by _check_required
        value = _clean(value)
        if value in seen:
            report.error(sheet, i, f"Duplicate {field} {value}", field=field)
        else:
            seen.add(value)


def _check_email(report: _Report, sheet: str, rows: List[dict]) -> None:
    for i, row in enumerate(rows, start=1):
        email = row.get("email")
        if _blank(email):
            continue
        email = _clean(email)
        at = email.find("@")
        if at <= 0 or "." not in email[at + 1:]:
            report.error(sheet, i, f"Invalid email {email}", field="email")


def _codes(rows: List[dict], field: str) -> set:
    return {_clean(r.get(field)) for r in rows if not _blank(r.get(field))}


def _check_players(report: _Report, rows: List[dict], team_codes: set) -> None:
    for i, row in enumerate(rows, start=1):
        team_code = row.get("team_code")
        if not _blank(team_code) and _clean(team_code) not in team_codes:
            report.error("players", i, f"Unknown team_code {_clean(team_code)}",
                        field="team_code")
        jersey = row.get("jersey_number")
        if _blank(jersey):
            continue
        try:
            valid = int(_clean(jersey)) > 0
        except ValueError:
            valid = False
        if not valid:
            report.error("players", i, f"Invalid jersey_number {jersey}",
                        field="jersey_number")


def _check_ice_slots(report: _Report, rows: List[dict], rink_codes: set) -> None:
    """Field-level checks, returning the (row, rink_code, start, end) tuples
    of slots that parsed cleanly enough to be worth an overlap check."""
    parsed = []
    for i, row in enumerate(rows, start=1):
        rink_code = row.get("rink_code")
        rink_code = None if _blank(rink_code) else _clean(rink_code)
        if rink_code is not None and rink_code not in rink_codes:
            report.error("ice_slots", i, f"Unknown rink_code {rink_code}",
                        field="rink_code")

        start = _parse_iso_utc(row.get("start_time"))
        if not _blank(row.get("start_time")) and start is None:
            report.error("ice_slots", i,
                        f"Invalid start_time {row.get('start_time')!r}",
                        field="start_time")
        end = _parse_iso_utc(row.get("end_time"))
        if not _blank(row.get("end_time")) and end is None:
            report.error("ice_slots", i,
                        f"Invalid end_time {row.get('end_time')!r}",
                        field="end_time")
        if start is not None and end is not None and end <= start:
            report.error("ice_slots", i, "end_time must be after start_time.",
                        field="end_time")

        slot_type = row.get("slot_type")
        if not _blank(slot_type) and _clean(slot_type) not in _VALID_SLOT_TYPES:
            allowed = ", ".join(sorted(_VALID_SLOT_TYPES))
            report.error("ice_slots", i,
                        f"Unknown slot_type {slot_type!r}. Allowed: {allowed}.",
                        field="slot_type")

        if rink_code is not None and start is not None and end is not None:
            parsed.append((i, rink_code, start, end))
    return parsed


def _check_overlaps(report: _Report, parsed_slots) -> None:
    """Same rink_code + overlapping [start, end) → a WARNING, not an error."""
    for idx, (row_a, rink_a, start_a, end_a) in enumerate(parsed_slots):
        for row_b, rink_b, start_b, end_b in parsed_slots[idx + 1:]:
            if rink_a != rink_b:
                continue
            if start_a < end_b and end_a > start_b:
                report.warning(
                    "ice_slots", row_a,
                    f"Slot overlaps another slot on the same rink (row {row_b}).")


def validate_import(sheets: Dict[str, List[dict]]) -> dict:
    """Validate spreadsheet-shaped import rows without touching the store.

    ``sheets`` maps sheet name -> list of row dicts; any of
    :data:`IMPORT_SHEET_NAMES` may be absent, treated as an empty sheet.
    Returns a report dict: ``{"ok", "summary", "errors", "warnings"}``.
    ``ok`` is true iff ``errors`` is empty — warnings never block it.
    """
    rows = {name: list(sheets.get(name) or []) for name in IMPORT_SHEET_NAMES}
    report = _Report()

    for name in IMPORT_SHEET_NAMES:
        _check_required(report, name, rows[name])
    for name in _UNIQUE_FIELD:
        _check_unique(report, name, rows[name])
    _check_email(report, "players", rows["players"])
    _check_email(report, "officials", rows["officials"])

    _check_players(report, rows["players"], _codes(rows["teams"], "team_code"))
    parsed_slots = _check_ice_slots(report, rows["ice_slots"],
                                    _codes(rows["rinks"], "rink_code"))
    _check_overlaps(report, parsed_slots)

    return {
        "ok": not report.errors,
        "summary": {name: len(rows[name]) for name in IMPORT_SHEET_NAMES},
        "errors": report.errors,
        "warnings": report.warnings,
    }
