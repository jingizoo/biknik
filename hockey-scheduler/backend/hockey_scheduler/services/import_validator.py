"""CSV import dry-run validator (#92).

Step 1 of the Pilot Onboarding Import Wizard: an operator uploads
spreadsheet-shaped rows for teams, players, officials, rinks, and ice slots
and gets back a structured validation report — WITHOUT writing anything to
the database. Commit/write flows are separate follow-up slices (#93-#96).

The original dry-run predates persisted ``external_ref`` codes. Validation is
still sheet-local for ordinary references, but #269 optionally supplies a store
for one read-only purpose: simulate the post-import Player jersey occupancy and
report collisions with existing active players. ``validate_import`` never writes;
mapping and commit remain #93's job.

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

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain.errors import DomainError
from ..domain import (
    IceSlotType,
    MAX_JERSEY_NUMBER,
    MIN_JERSEY_NUMBER,
    OfficialAvailabilityStatus,
    PolicyScopeType,
    intervals_overlap,
    parse_jersey_cell,
)
from .ice_availability import curfew_instant, parse_hhmm

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
_VALID_AVAILABILITY_STATUSES = {s.value for s in OfficialAvailabilityStatus}

_REQUIRED_AVAILABILITY_FIELDS = (
    "official_code", "start_time", "end_time", "status")


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


def _check_players(report: _Report, rows: List[dict], team_codes: set,
                   store=None) -> None:
    """Validate Player rows, including their post-import jersey occupancy.

    The legacy preview originally had no store context.  When a store is
    supplied, resolve uploaded team/player codes to the rows that the commit
    path would update and compare the resulting active ``(team, jersey)``
    state with existing players.  Grouping first lets us report *every* upload
    row in a collision instead of only the second and later rows.
    """
    existing_teams = {}
    existing_players = {}
    occupancy = {}
    upload_player_codes = {
        _clean(row.get("player_code")) for row in rows
        if not _blank(row.get("player_code"))}
    if store is not None:
        existing_teams = {
            team.external_ref: team for team in store.all_teams()
            if team.external_ref}
        existing_players = {
            player.external_ref: player for player in store.all_players()
            if player.external_ref}
        # Uploaded existing players are removed before being placed at their
        # target team/jersey below; otherwise an unchanged re-import would
        # falsely collide with itself.
        for player in store.all_players():
            if (not player.is_active or player.jersey_number is None
                    or (player.external_ref
                        and player.external_ref in upload_player_codes)):
                continue
            occupancy.setdefault(
                (player.team_id, player.jersey_number), []).append(player)

    grouped = {}
    for i, row in enumerate(rows, start=1):
        team_code = row.get("team_code")
        team_code_clean = None if _blank(team_code) else _clean(team_code)
        if team_code_clean is not None and team_code_clean not in team_codes:
            report.error("players", i, f"Unknown team_code {team_code_clean}",
                        field="team_code")
        jersey_number, reason = parse_jersey_cell(row.get("jersey_number"))
        if reason is not None:
            report.error(
                "players", i,
                f"Invalid jersey_number {row.get('jersey_number')} "
                f"(must be {MIN_JERSEY_NUMBER}-{MAX_JERSEY_NUMBER}).",
                field="jersey_number")
            continue
        if team_code_clean is None:
            continue

        player_code = (None if _blank(row.get("player_code"))
                       else _clean(row.get("player_code")))
        existing = existing_players.get(player_code)
        # The legacy commit leaves a blank jersey unchanged on update and never
        # reactivates an inactive player, so mirror that exact target state.
        target_jersey = (existing.jersey_number
                         if jersey_number is None and existing is not None
                         else jersey_number)
        target_active = True if existing is None else existing.is_active
        if target_jersey is None or not target_active:
            continue
        team = existing_teams.get(team_code_clean)
        team_key = team.id if team is not None else ("upload", team_code_clean)
        grouped.setdefault((team_key, target_jersey), []).append(
            (i, team_code_clean, player_code or f"row {i}"))

    for key, participants in grouped.items():
        existing_holders = occupancy.get(key, [])
        if len(participants) < 2 and not existing_holders:
            continue
        upload_rows = ", ".join(str(item[0]) for item in participants)
        holder_text = ""
        if existing_holders:
            holders = ", ".join(
                f"{player.name} ({player.id})" for player in existing_holders)
            holder_text = f"; existing active player(s): {holders}"
        for row_number, team_code, _player_code in participants:
            report.error(
                "players", row_number,
                f"Duplicate jersey_number {key[1]} on team {team_code}; "
                f"conflicting upload row(s): {upload_rows}{holder_text}.",
                field="jersey_number")


def _check_ice_slots(report: _Report, rows: List[dict], rink_codes: set) -> None:
    """Field-level checks, returning the (row, rink_code, start, end) tuples
    of slots that parsed cleanly enough to be worth an overlap check, plus a
    row-index -> slot_type map for the policy advisory's game-vs-game filter
    (#277: buffers are a game rule; maintenance/practice ice never hosts
    one, so it must not trigger 'games on both cannot coexist')."""
    parsed = []
    slot_types = {}
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
            # Blank defaults to "game", exactly like the commit path.
            slot_types[i] = _clean(slot_type) if not _blank(slot_type) else "game"
    return parsed, slot_types


def _ingest_policy(store, rink):
    """The scheduling policy an INGEST advisory can honestly resolve for a
    persisted rink (#277 Slice B): the rink-scope row, field-merged over the
    program-scope row via the venue's legacy one-venue-one-program bridge
    (``Venue.league_id``). Season scope deliberately does NOT apply — an ice
    slot is rink-scoped and belongs to no season at ingest, so a season-level
    policy cannot be attributed to it here; placement-time enforcement (which
    knows the game's season) remains authoritative. Returns the merged field
    dict, the venue timezone name (for curfew wall clocks), and a per-field
    SOURCE map ("rink" | "program" | None) — the wording hinge (#319
    review): the gate resolves Rink > Season > Program, so a rink-sourced
    value binds whatever season a future game brings, while a
    program-sourced value is only a fallback that game's own Season may
    override, and an advisory must not promise refusal from it."""
    rink_row = store.find_scheduling_policy(PolicyScopeType.RINK, rink.id)
    venue = store.get_venue(rink.venue_id) if rink.venue_id else None
    program_id = getattr(venue, "league_id", None)
    program_row = None
    if program_id and store.get_program(program_id) is not None:
        program_row = store.find_scheduling_policy(
            PolicyScopeType.PROGRAM, program_id)
    values, sources = {}, {}
    for field in ("warmup_minutes", "resurfacing_minutes",
                  "min_playable_minutes", "curfew_local"):
        rv = getattr(rink_row, field, None) if rink_row else None
        pv = getattr(program_row, field, None) if program_row else None
        if rv is not None:
            values[field], sources[field] = rv, "rink"
        elif pv is not None:
            values[field], sources[field] = pv, "program"
        else:
            values[field], sources[field] = None, None
    # Curfew wall-clock anchor: venue timezone, else the bridged Program's
    # — the SAME fallback chain as the gate's _curfew_timezone, so an
    # advisory can never disagree with enforcement about which clock a
    # curfew uses (UTC last, in the caller).
    program = store.get_program(program_id) if program_id else None
    tz_name = (getattr(venue, "timezone", None)
               or getattr(program, "timezone", None))
    return values, tz_name, sources


def _hosted_effective_policy(store, rink, season_id):
    """The placement gate's exact Rink > Season > Program field merge for a
    game whose season the store already knows (#319 review): the season's
    OWN program — not the venue bridge — supplies the Program scope, so an
    advisory about an existing HOSTED slot predicts precisely what the
    gate will enforce."""
    rows = [store.find_scheduling_policy(PolicyScopeType.RINK, rink.id)]
    season = store.get_season(season_id) if season_id else None
    if season is not None:
        rows.append(store.find_scheduling_policy(
            PolicyScopeType.SEASON, season.id))
        if season.program_id:
            rows.append(store.find_scheduling_policy(
                PolicyScopeType.PROGRAM, season.program_id))
    values = {}
    for field in ("warmup_minutes", "resurfacing_minutes",
                  "min_playable_minutes", "curfew_local"):
        values[field] = next(
            (getattr(r, field) for r in rows
             if r is not None and getattr(r, field) is not None), None)
    return values


def _check_policy_advisories(report: _Report, parsed_slots, slot_types,
                             store) -> None:
    """#277 Slice B ingest advisories — WARNINGS, never errors: contracted
    start/end values import untouched (the issue's no-silent-time-shift
    mandate), but a row the commit or the placement gate will later refuse
    is flagged now. Four checks per row against rinks that already exist
    in the store (a rink created by this same upload can have no policy
    yet): physical overlap with any PERSISTED slot (#319 review —
    definitive for EVERY slot-type combination, mirroring the commit's
    slot_type-agnostic overlap refusal, with the exact-tuple re-import
    identity excluded), playable span shorter than the minimum (#319
    review — worded as a definitive refusal only when the minimum is
    RINK-sourced, which no future game's season can override; a
    program-sourced fallback is labeled a Program-level advisory whose
    outcome the game's own Season decides), same-rink DIRECTIONAL turnover
    proximity (#319 review — the earlier side's resurfacing plus the later
    side's warm-up, each from that side's honest resolution: an existing
    slot HOSTING an active game contributes its game's full effective
    Rink/Season/Program policy exactly like the gate, while the incoming
    row and any season-less counterpart resolve at rink level via
    ``_ingest_policy``; a warning is worded as a DEFINITIVE gate refusal
    only when the hosting game's own side alone already exceeds the gap —
    every violation that depends on the incoming row's rink-level guess is
    LABELED a rink-level advisory instead, since a future game's season
    can shadow that guess), and end past curfew (venue timezone, half-open
    exactly like enforcement via the shared curfew_instant; the same
    rink-sourced-definitive vs program-fallback-advisory wording split as
    the minimum)."""
    rinks_by_ref = {r.external_ref: r for r in store.all_rinks()
                    if r.external_ref}
    # -- physical overlap with PERSISTED ice: definitive for EVERY row and
    # every slot type (#319 review). The commit's overlap gate is
    # slot_type-agnostic, so the preview must be too — a practice row over
    # persisted game ice (or a game row over persisted practice ice) is
    # hard-refused at commit and must never preview clean. Mirrors that
    # gate exactly: a row whose exact (rink, start, end) tuple is already
    # persisted takes the commit's in-place update path and is never an
    # overlap — even against OTHER persisted ice, because the gate
    # short-circuits the same way; intervals_overlap is half-open, so
    # exact adjacency never lands here. Same-upload row-vs-row overlap is
    # _check_overlaps' separate unconditional warning — such pairs COMMIT
    # (nothing is persisted yet), so they must not claim refusal here.
    persisted_by_rink = {}
    for ex in store.all_ice_slots():
        persisted_by_rink.setdefault(ex.rink_id, []).append(ex)
    for row, rink_code, start, end in parsed_slots:
        rink = rinks_by_ref.get(rink_code)
        if rink is None:
            continue  # a brand-new rink can't yet have persisted ice
        persisted = persisted_by_rink.get(rink.id, [])
        if any(ex.start_time == start and ex.end_time == end
               for ex in persisted):
            continue  # exact tuple -> the commit's update path, no overlap
        clash = next((ex for ex in persisted if intervals_overlap(
            start, end, ex.start_time, ex.end_time)), None)
        if clash is not None:
            report.warning(
                "ice_slots", row,
                f"Slot physically overlaps existing slot {clash.id} on "
                f"rink {rink_code} — persisted ice is never silently "
                "rewritten, so the commit will refuse this row.")
    policy_cache = {}
    hosted_policy_cache = {}  # (rink_id, season_id) -> effective fields
    # An active game — committed drafts count, exactly like the gate —
    # pins its slot's side of the buffer to ITS season's effective policy.
    hosted = {g.ice_slot_id: g for g in store.all_games()
              if g.ice_slot_id and not g.cancelled}
    for row, rink_code, start, end in parsed_slots:
        if slot_types.get(row) != "game":
            continue  # maintenance/practice ice never hosts a game
        rink = rinks_by_ref.get(rink_code)
        if rink is None:
            continue
        if rink.id not in policy_cache:
            policy_cache[rink.id] = _ingest_policy(store, rink)
        values, tz_name, sources = policy_cache[rink.id]
        min_playable = values["min_playable_minutes"] or 0
        slot_minutes = int((end - start).total_seconds() // 60)
        if slot_minutes < min_playable:
            # Definitive ONLY when rink-sourced (#319 review): Rink outranks
            # Season at the gate, so no future game's season can soften it.
            # A program-sourced minimum is a fallback the game's own Season
            # (or its season's different Program) may override — promising
            # refusal from it would be false.
            if sources["min_playable_minutes"] == "rink":
                report.warning(
                    "ice_slots", row,
                    f"Slot is only {slot_minutes} playable minutes; rink "
                    f"{rink_code}'s scheduling policy requires at least "
                    f"{min_playable} — it will be refused a game.")
            elif sources["min_playable_minutes"] == "program":
                report.warning(
                    "ice_slots", row,
                    f"Program-level advisory: slot is only {slot_minutes} "
                    f"playable minutes against the program fallback minimum "
                    f"of {min_playable} on rink {rink_code} — the exact "
                    "outcome depends on the future game's season, whose own "
                    "policy may override the program, so it may still be "
                    "schedulable.")
            # else: no policy resolved this field at all (source is None,
            # min_playable defaults to 0) — the only way to still trip the
            # check is a malformed end<=start row, already error-reported
            # above; there is no actual Program (or any) minimum to advise
            # about, so it must not fabricate a "program fallback" claim.
        # -- turnover proximity, DIRECTIONAL per side (#319 review): the
        # required gap is the EARLIER side's resurfacing + the LATER side's
        # warm-up, same boundary rule as the gate (STRICTLY closer
        # conflicts; a gap exactly equal is compliant and silent). The
        # incoming row's side always resolves at rink level (an ice slot
        # belongs to no season at ingest); the counterpart's side resolves
        # from its hosting active game's effective policy when there is
        # one, else at rink level too.
        def _gap_required_split(o_start, o_end, other_values):
            """(gap, required, other_side): the directional pair math plus
            the COUNTERPART's own contribution alone — the only part a
            hosted advisory may treat as certain, since the incoming row's
            side is a rink-level guess a future season can shadow. Both
            callers exclude physically overlapping pairs before the call
            (overlap is reported definitively elsewhere and must never
            reach buffer math), so the spans are strictly disjoint here.
            """
            if o_start >= end:   # incoming earlier: its resurfacing +
                gap = (o_start - end).total_seconds() // 60
                other_side = other_values["warmup_minutes"] or 0
                required = ((values["resurfacing_minutes"] or 0)
                            + other_side)
            else:                # incoming later: counterpart's resurfacing
                gap = (start - o_end).total_seconds() // 60
                other_side = other_values["resurfacing_minutes"] or 0
                required = (other_side
                            + (values["warmup_minutes"] or 0))
            return int(gap), required, other_side

        near_hosted, near_rink_level = [], []
        for other_row, other_code, o_start, o_end in parsed_slots:
            if other_row == row or other_code != rink_code:
                continue
            if slot_types.get(other_row) != "game":
                continue  # buffers are game-vs-game, like the gate
            if intervals_overlap(start, end, o_start, o_end):
                # Same-upload overlap is _check_overlaps' unconditional
                # warning; overlapping spans must never reach the
                # directional buffer math below.
                continue
            gap, required, _side = _gap_required_split(o_start, o_end,
                                                       values)
            if required > 0 and 0 <= gap < required:
                near_rink_level.append((f"row {other_row}", gap, required))
        for ex in store.all_ice_slots():
            if ex.rink_id != rink.id:
                continue
            if getattr(ex.slot_type, "value", ex.slot_type) != "game":
                continue  # buffers are game-vs-game, like the gate
            if ex.start_time == start and ex.end_time == end:
                # The row's own persisted copy — a re-import updates
                # this exact tuple in place (the commit path's
                # documented identity), so warning against it would
                # falsely flag every idempotent re-import
                # (_check_players self-excludes for the same reason).
                continue
            if intervals_overlap(start, end, ex.start_time, ex.end_time):
                # Already reported by the dedicated slot_type-agnostic
                # overlap pass above; overlapping spans must never reach
                # the directional buffer math below.
                continue
            host = hosted.get(ex.id)
            if host is not None:
                hkey = (rink.id, host.season_id)
                if hkey not in hosted_policy_cache:
                    hosted_policy_cache[hkey] = _hosted_effective_policy(
                        store, rink, host.season_id)
                other_values = hosted_policy_cache[hkey]
            else:
                other_values = values
            gap, required, host_side = _gap_required_split(
                ex.start_time, ex.end_time, other_values)
            if required > 0 and 0 <= gap < required:
                # DEFINITIVE only when the hosting game's OWN side alone
                # already exceeds the gap (then the gate refuses whatever
                # season the imported row's future game resolves); a
                # violation that needs the incoming row's rink-level guess
                # stays an advisory.
                if host is not None and gap < host_side:
                    near_hosted.append(
                        (f"existing slot {ex.id}", gap, host_side))
                else:
                    near_rink_level.append(
                        (f"existing slot {ex.id}", gap, required))
        if near_hosted:
            what, gap, needed = near_hosted[0]
            report.warning(
                "ice_slots", row,
                f"Slot sits only {gap} min from {what} on rink "
                f"{rink_code}, which hosts a game whose scheduling policy "
                f"needs {needed} min on its side of the pair — the "
                "placement gate will refuse a game here whatever season "
                "the new row's games belong to.")
        elif near_rink_level:
            what, gap, required = near_rink_level[0]
            report.warning(
                "ice_slots", row,
                f"Rink-level advisory: slot sits only {gap} min from "
                f"{what} on rink {rink_code}; the effective policies need "
                f"{required} min of resurfacing + warm-up between games, "
                "so games on both may not coexist (the games' seasons "
                "decide the exact requirement).")
        curfew = values["curfew_local"]
        if curfew:
            try:
                # Stored values are normalized "HH:MM" (set_scheduling_policy),
                # but a dry-run must degrade to no-advisory on any corrupt
                # legacy value, never 500.
                hour, minute = parse_hhmm(curfew, "curfew_local")
            except DomainError:
                continue
            try:
                tz = ZoneInfo(tz_name) if tz_name else timezone.utc
            except (ZoneInfoNotFoundError, ValueError, OSError):
                tz = timezone.utc
            start_local = start.astimezone(tz)
            deadline = curfew_instant(start_local, hour, minute)
            if end > deadline.astimezone(timezone.utc):
                # Same sourcing hinge as the minimum above (#319 review).
                if sources["curfew_local"] == "rink":
                    report.warning(
                        "ice_slots", row,
                        f"Slot ends at "
                        f"{end.astimezone(tz).strftime('%H:%M')} local, past "
                        f"rink {rink_code}'s {curfew} curfew — it will be "
                        "refused a game.")
                else:
                    report.warning(
                        "ice_slots", row,
                        f"Program-level advisory: slot ends at "
                        f"{end.astimezone(tz).strftime('%H:%M')} local, past "
                        f"the program fallback curfew {curfew} on rink "
                        f"{rink_code} — the exact outcome depends on the "
                        "future game's season, whose own policy may "
                        "override the program, so it may still be "
                        "schedulable.")


def _check_overlaps(report: _Report, parsed_slots) -> None:
    """Same rink_code + overlapping [start, end) → a WARNING, not an error."""
    for idx, (row_a, rink_a, start_a, end_a) in enumerate(parsed_slots):
        for row_b, rink_b, start_b, end_b in parsed_slots[idx + 1:]:
            if rink_a != rink_b:
                continue
            if intervals_overlap(start_a, end_a, start_b, end_b):
                report.warning(
                    "ice_slots", row_a,
                    f"Slot overlaps another slot on the same rink (row {row_b}).")


def validate_import(sheets: Dict[str, List[dict]], store=None) -> dict:
    """Validate spreadsheet-shaped import rows without writing to the store.

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

    _check_players(report, rows["players"],
                   _codes(rows["teams"], "team_code"), store=store)
    parsed_slots, slot_types = _check_ice_slots(
        report, rows["ice_slots"], _codes(rows["rinks"], "rink_code"))
    _check_overlaps(report, parsed_slots)
    if store is not None:
        _check_policy_advisories(report, parsed_slots, slot_types, store)

    return {
        "ok": not report.errors,
        "summary": {name: len(rows[name]) for name in IMPORT_SHEET_NAMES},
        "errors": report.errors,
        "warnings": report.warnings,
    }


def validate_official_availability(rows: List[dict], official_codes_in_sheet: set,
                                    existing_external_refs: set) -> dict:
    """Validate ``official_availability`` sheet rows (#94).

    A NEW sibling to :func:`validate_import`, not a change to it — that
    function's own ``officials`` sheet checks are reused unchanged by the
    caller (``SetupService.commit_officials_availability_import``) by passing
    it only the ``officials`` key.

    Deliberately DIFFERENT from #92/#93's sheet-internal-only cross-reference
    rule: a row's ``official_code`` may resolve against EITHER (a) the
    ``officials`` sheet of the SAME upload, OR (b) an official already
    persisted from a PRIOR commit (``existing_external_refs``). #93's
    team_code/rink_code were sheet-internal-only because there was no
    persisted external-code concept yet; now that ``external_ref`` persists
    for officials too (as of this PR), a real pilot workflow is "import
    officials once, then import availability windows in a separate later
    commit without re-sending officials.csv every time" — so this function
    takes both sets and checks against their union. Flagged here for the
    reviewer as an intentional divergence, not an oversight.

    ``rows`` are already-parsed row dicts (same convention as
    ``validate_import``); row numbers are 1-indexed against these data rows.
    Returns ``{"errors": [...], "warnings": [...]}`` in the same shape as
    ``validate_import``'s errors/warnings.
    """
    report = _Report()
    sheet = "official_availability"
    known_codes = official_codes_in_sheet | existing_external_refs

    for i, row in enumerate(rows, start=1):
        for field in _REQUIRED_AVAILABILITY_FIELDS:
            if _blank(row.get(field)):
                report.error(sheet, i, f"{field} is required.", field=field)

    parsed = []
    for i, row in enumerate(rows, start=1):
        code = row.get("official_code")
        code = None if _blank(code) else _clean(code)
        if code is not None and code not in known_codes:
            report.error(sheet, i, f"Unknown official_code {code}",
                        field="official_code")

        start = _parse_iso_utc(row.get("start_time"))
        if not _blank(row.get("start_time")) and start is None:
            report.error(sheet, i,
                        f"Invalid start_time {row.get('start_time')!r}",
                        field="start_time")
        end = _parse_iso_utc(row.get("end_time"))
        if not _blank(row.get("end_time")) and end is None:
            report.error(sheet, i,
                        f"Invalid end_time {row.get('end_time')!r}",
                        field="end_time")
        if start is not None and end is not None and end <= start:
            report.error(sheet, i, "end_time must be after start_time.",
                        field="end_time")

        status = row.get("status")
        if not _blank(status) and _clean(status) not in _VALID_AVAILABILITY_STATUSES:
            allowed = ", ".join(sorted(_VALID_AVAILABILITY_STATUSES))
            report.error(sheet, i,
                        f"Unknown status {status!r}. Allowed: {allowed}.",
                        field="status")

        if code is not None and start is not None and end is not None:
            parsed.append((i, code, start, end))

    for idx, (row_a, code_a, start_a, end_a) in enumerate(parsed):
        for row_b, code_b, start_b, end_b in parsed[idx + 1:]:
            if code_a != code_b:
                continue
            if intervals_overlap(start_a, end_a, start_b, end_b):
                report.warning(
                    sheet, row_a,
                    f"Availability window overlaps another window for the "
                    f"same official (row {row_b}).")

    return {"errors": report.errors, "warnings": report.warnings}
