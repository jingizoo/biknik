"""Idempotent client hierarchy CSV import (#174 PR E2, #260 Slice F).

Nine CSV sheets describe the durable setup hierarchy and its import identity
before games exist: organizations (facility owners), programs, venues+rinks,
competition (season/league/optional division), clubs, permanent teams,
players, registrations, and season venue access. Validation is read-only and
resolves references against both the same upload and existing external_ref
values (or, for SeasonTeamRegistration/SeasonVenueAccess, the composite key
they're actually identified by — neither has an external_ref of its own).
Commit re-runs validation and applies the whole batch in a single store
transaction; missing rows are never treated as deletes.

Column vocabulary follows ADR 0001 (docs/architecture/decisions/
0001-competition-model-reset.md) and issue #260's locked design review:
``leagues`` (old) is ``programs`` (new); old ``level_*`` is new ``league_*``;
``registrations`` requires an explicit ``league_code`` rather than deriving
it from a division or "the season's only league"; ``venues_rinks`` no longer
carries a program/league column at all — Venue.league_id (the pre-Slice-E
bridge) is never written by this importer.
"""

from collections import OrderedDict
from typing import Dict, Iterable, List, Optional, Tuple

from ..domain.enums import Position


HIERARCHY_SHEET_NAMES = (
    "organizations", "programs", "venues_rinks", "competition", "clubs",
    "permanent_teams", "players", "registrations", "season_venue_access")
HIERARCHY_CSV_KEYS = tuple(f"{name}_csv" for name in HIERARCHY_SHEET_NAMES)

HIERARCHY_TEMPLATES = {
    "organizations_csv": (
        "organization_code,organization_name,short_name\n"
        "CANLON,Canlon Ice Facilities,Canlon\n"
    ),
    "programs_csv": (
        "program_code,operator_organization_code,program_name,country,timezone\n"
        "OVER55,CANLON,Over 55,US,America/Chicago\n"
    ),
    # No program/league column — a Venue is owned by its facility
    # organization only (#233 Slice E); the legacy one-Venue-one-Program
    # bridge (Venue.league_id) is never written here.
    "venues_rinks_csv": (
        "venue_code,organization_code,venue_name,address,timezone,rink_code,rink_name\n"
        "PLAINFIELD,CANLON,Plainfield Ice,123 Main St,America/Chicago,PF1,Rink 1\n"
    ),
    "competition_csv": (
        "program_code,season_code,season_name,league_code,league_name,"
        "league_sort_order,division_code,division_name,age_group\n"
        "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult\n"
    ),
    # Club (#260): matched by its own stable club_code, never by name — a
    # renamed Club (same code, new club_name) updates the existing record.
    "clubs_csv": (
        "club_code,club_name,country\n"
        "EAGLES,Eagles HC,US\n"
    ),
    # Permanent league teams (#180, extended #260): a team is a permanent
    # member of a Program, keyed by team_code; it carries no division here —
    # season participation is a separate registration sheet below. club_code
    # is optional: blank/NA means club_id=null, never a placeholder Club.
    "permanent_teams_csv": (
        "program_code,team_code,team_name,club_code\n"
        "OVER55,LIONS,Lions,EAGLES\n"
    ),
    # Player (#260): stable player_code -> Team. email is optional; when
    # supplied it may create/update the same player:<id> ContactDestination
    # manual creation already produces.
    "players_csv": (
        "player_code,team_code,first_name,last_name,jersey_number,position,email\n"
        "P001,LIONS,Jane,Smith,7,forward,jane@example.com\n"
    ),
    # Season registrations (#180, #260): one row per team's participation in
    # a season. league_code is REQUIRED (issue #260 review decision 2) — an
    # old-format file omitting it gets a clear dry-run error, never a silent
    # derivation from division or "the season's only league". division_code
    # remains optional.
    "registrations_csv": (
        "season_code,team_code,league_code,division_code\n"
        "FALL26,LIONS,L1,DIVA\n"
    ),
    # Season venue access (#260, new): grants (or revokes) a Season's access
    # to a Venue's ice via SeasonVenueAccess — never Venue.league_id. Blank
    # active defaults to true.
    "season_venue_access_csv": (
        "season_code,venue_code,active\n"
        "FALL26,PLAINFIELD,true\n"
    ),
}

_REQUIRED = {
    "organizations": ("organization_code", "organization_name"),
    "programs": ("program_code", "program_name"),
    "venues_rinks": (
        "venue_code", "organization_code", "venue_name",
        "rink_code", "rink_name"),
    "competition": (
        "program_code", "season_code", "season_name",
        "league_code", "league_name"),
    "clubs": ("club_code", "club_name"),
    "permanent_teams": ("program_code", "team_code", "team_name"),
    "players": ("player_code", "team_code", "first_name", "last_name",
               "position"),
    "registrations": ("season_code", "team_code", "league_code"),
    "season_venue_access": ("season_code", "venue_code"),
}

# Overrides the generic "<field> is required." message for a handful of
# fields where the plain message would leave an operator re-uploading an
# old-format file guessing why (#260 review decision 2).
_REQUIRED_MESSAGES = {
    ("registrations", "league_code"): (
        "league_code is required. The registrations sheet's canonical "
        "format is season_code,team_code,league_code,division_code — "
        "an explicit League is required and is never derived from a "
        "Division or \"the season's only league\"."),
}


def _blank(value) -> bool:
    return value is None or not str(value).strip()


def _clean(value, default="") -> str:
    return default if _blank(value) else str(value).strip()


def _optional(value) -> Optional[str]:
    return None if _blank(value) else str(value).strip()


def _int(value, default=0) -> int:
    return default if _blank(value) else int(str(value).strip())


def _bool(value, default=True) -> bool:
    if _blank(value):
        return default
    return str(value).strip().lower() not in ("false", "0", "no")


class _Report:
    def __init__(self):
        self.errors: List[dict] = []
        self.warnings: List[dict] = []

    def error(self, sheet: str, row: int, code: str, message: str,
              field: Optional[str] = None) -> None:
        item = {"sheet": sheet, "row": row, "code": code, "message": message}
        if field:
            item["field"] = field
        self.errors.append(item)

    def warning(self, sheet: str, row: int, code: str, message: str,
               field: Optional[str] = None) -> None:
        item = {"sheet": sheet, "row": row, "code": code, "message": message}
        if field:
            item["field"] = field
        self.warnings.append(item)


def _existing_map(report: _Report, rows: Iterable, entity: str) -> dict:
    out = {}
    for obj in rows:
        code = _optional(getattr(obj, "external_ref", None))
        if not code:
            continue
        if code in out:
            report.error(
                "existing_data", 0, "duplicate_external_code",
                f"Multiple existing {entity} records use external code "
                f"{code}.", field=f"{entity}_code")
        else:
            out[code] = obj
    return out


def _check_required(report: _Report, sheet: str, rows: List[dict]) -> None:
    for index, row in enumerate(rows, start=1):
        for field in _REQUIRED[sheet]:
            if _blank(row.get(field)):
                message = _REQUIRED_MESSAGES.get(
                    (sheet, field), f"{field} is required.")
                report.error(sheet, index, f"{field}_required", message, field)


def _check_unique(report: _Report, sheet: str, rows: List[dict],
                  field: str) -> None:
    seen = set()
    for index, row in enumerate(rows, start=1):
        code = _optional(row.get(field))
        if not code:
            continue
        if code in seen:
            report.error(sheet, index, "duplicate_code",
                        f"Duplicate {field} {code}.", field)
        seen.add(code)


def _consistent_groups(report: _Report, sheet: str, rows: List[dict],
                       code_field: str, compared_fields: Tuple[str, ...]) -> None:
    seen = {}
    for index, row in enumerate(rows, start=1):
        code = _optional(row.get(code_field))
        if not code:
            continue
        values = tuple(_clean(row.get(field)) for field in compared_fields)
        if code in seen and seen[code] != values:
            report.error(
                sheet, index, "inconsistent_fields",
                f"Rows using {code_field} {code} disagree on "
                f"{', '.join(compared_fields)}.", code_field)
        else:
            seen[code] = values


def _group_first(rows: List[dict], code_field: str) -> OrderedDict:
    grouped = OrderedDict()
    for row in rows:
        code = _optional(row.get(code_field))
        if code and code not in grouped:
            grouped[code] = row
    return grouped


def validate_hierarchy_import(sheets: Dict[str, List[dict]], store) -> dict:
    """Validate every hierarchy reference before any write.

    References may resolve from another sheet in this upload or from a unique,
    already-persisted ``external_ref`` (or, for SeasonTeamRegistration/
    SeasonVenueAccess, an existing composite key). The function only reads
    the store. Returns every row's errors in one pass — it never stops at
    the first failure (#260 review).
    """
    rows = {name: list((sheets or {}).get(name) or [])
            for name in HIERARCHY_SHEET_NAMES}
    report = _Report()
    if not any(rows.values()):
        report.error("hierarchy", 0, "empty_upload",
                    "At least one hierarchy CSV row is required.")

    for name in HIERARCHY_SHEET_NAMES:
        _check_required(report, name, rows[name])

    _check_unique(report, "organizations", rows["organizations"],
                  "organization_code")
    _check_unique(report, "programs", rows["programs"], "program_code")
    _check_unique(report, "venues_rinks", rows["venues_rinks"], "rink_code")
    _check_unique(report, "competition", rows["competition"], "division_code")
    _check_unique(report, "clubs", rows["clubs"], "club_code")
    _check_unique(report, "permanent_teams", rows["permanent_teams"],
                  "team_code")
    _check_unique(report, "players", rows["players"], "player_code")

    _consistent_groups(
        report, "venues_rinks", rows["venues_rinks"], "venue_code",
        ("organization_code", "venue_name", "address", "timezone"))
    _consistent_groups(
        report, "competition", rows["competition"], "season_code",
        ("program_code", "season_name"))
    league_rows = [row for row in rows["competition"]
                  if not _blank(row.get("league_code"))]
    _consistent_groups(
        report, "competition", league_rows, "league_code",
        ("season_code", "league_name", "league_sort_order"))
    _consistent_groups(
        report, "permanent_teams", rows["permanent_teams"], "team_code",
        ("program_code", "team_name", "club_code"))
    # season_venue_access has no external_ref of its own; its (season_code,
    # venue_code) pair-consistency (do repeated rows for the same pair agree
    # on active?) is checked further down, once venue codes are known.

    existing_orgs = _existing_map(
        report, store.all_organizations(), "organization")
    existing_programs = _existing_map(report, store.all_programs(), "program")
    existing_venues = _existing_map(report, store.all_venues(), "venue")
    _existing_map(report, store.all_rinks(), "rink")
    existing_seasons = _existing_map(report, store.all_seasons(), "season")
    existing_leagues = _existing_map(report, store.all_leagues(), "league")
    existing_divisions = _existing_map(report, store.all_divisions(), "division")
    existing_clubs = _existing_map(report, store.all_clubs(), "club")
    existing_teams = _existing_map(report, store.all_teams(), "team")
    existing_players = _existing_map(report, store.all_players(), "player")

    upload_org_codes = {
        _clean(row.get("organization_code")) for row in rows["organizations"]
        if not _blank(row.get("organization_code"))}
    known_org_codes = upload_org_codes | set(existing_orgs)

    for index, row in enumerate(rows["programs"], start=1):
        org_code = _optional(row.get("operator_organization_code"))
        if org_code and org_code not in known_org_codes:
            report.error("programs", index, "unknown_organization_code",
                         f"Unknown operator_organization_code {org_code}.",
                         "operator_organization_code")

    upload_program_codes = {
        _clean(row.get("program_code")) for row in rows["programs"]
        if not _blank(row.get("program_code"))}
    known_program_codes = upload_program_codes | set(existing_programs)

    for index, row in enumerate(rows["venues_rinks"], start=1):
        org_code = _optional(row.get("organization_code"))
        if org_code and org_code not in known_org_codes:
            report.error("venues_rinks", index, "unknown_organization_code",
                         f"Unknown organization_code {org_code}.",
                         "organization_code")

    for index, row in enumerate(rows["competition"], start=1):
        program_code = _optional(row.get("program_code"))
        if program_code and program_code not in known_program_codes:
            report.error("competition", index, "unknown_program_code",
                         f"Unknown program_code {program_code}.",
                         "program_code")
        if not _blank(row.get("league_sort_order")):
            try:
                int(_clean(row.get("league_sort_order")))
            except ValueError:
                report.error(
                    "competition", index, "invalid_league_sort_order",
                    f"Invalid league_sort_order "
                    f"{row.get('league_sort_order')!r}.",
                    "league_sort_order")
        division_code = _optional(row.get("division_code"))
        division_name = _optional(row.get("division_name"))
        if bool(division_code) != bool(division_name):
            report.error(
                "competition", index, "division_fields_incomplete",
                "division_code and division_name must be supplied together.",
                "division_code" if not division_code else "division_name")

    # Clubs referenced by permanent_teams must resolve; blank/NA means no
    # club (never a placeholder Club) — a non-blank code must be real.
    upload_club_codes = {
        _clean(row.get("club_code")) for row in rows["clubs"]
        if not _blank(row.get("club_code"))}
    known_club_codes = upload_club_codes | set(existing_clubs)
    for index, row in enumerate(rows["permanent_teams"], start=1):
        club_code = _optional(row.get("club_code"))
        if club_code and club_code not in known_club_codes:
            report.error("permanent_teams", index, "unknown_club_code",
                         f"Unknown club_code {club_code}.", "club_code")
        program_code = _optional(row.get("program_code"))
        if program_code and program_code not in known_program_codes:
            report.error("permanent_teams", index, "unknown_program_code",
                         f"Unknown program_code {program_code}.",
                         "program_code")

    # -- players (#260) ------------------------------------------------------
    upload_team_codes = {
        _clean(row.get("team_code")) for row in rows["permanent_teams"]
        if not _blank(row.get("team_code"))}
    known_team_codes_for_players = upload_team_codes | set(existing_teams)
    valid_positions = {p.value for p in Position}
    for index, row in enumerate(rows["players"], start=1):
        team_code = _optional(row.get("team_code"))
        if team_code and team_code not in known_team_codes_for_players:
            report.error("players", index, "unknown_team_code",
                         f"Unknown team_code {team_code}.", "team_code")
        jersey = _optional(row.get("jersey_number"))
        if jersey:
            try:
                if int(jersey) <= 0:
                    raise ValueError
            except ValueError:
                report.error("players", index, "invalid_jersey_number",
                            f"Invalid jersey_number {jersey!r}.",
                            "jersey_number")
        position = _optional(row.get("position"))
        if position and position.lower() not in valid_positions:
            report.error("players", index, "invalid_position",
                         f"Unknown position {position!r}.", "position")
        email = _optional(row.get("email"))
        if email:
            at = email.find("@")
            if at <= 0 or "." not in email[at + 1:]:
                report.error("players", index, "invalid_email",
                            f"Invalid email {email!r}.", "email")

    # -- permanent teams + registrations + season venue access --------------
    # Codes may resolve from this upload or from existing external_refs.
    # Build each program/season/league's parent chain so a registration or
    # venue-access row can be checked before any write.
    program_ref_by_id = {p.id: code for code, p in existing_programs.items()}
    season_ref_by_id = {s.id: code for code, s in existing_seasons.items()}
    league_ref_by_id = {lg.id: code for code, lg in existing_leagues.items()}

    season_program = {}
    for row in rows["competition"]:
        code = _optional(row.get("season_code"))
        if code:
            season_program.setdefault(code, _optional(row.get("program_code")))
    for code, season in existing_seasons.items():
        season_program.setdefault(code, program_ref_by_id.get(season.program_id))

    league_season = {}
    for row in league_rows:
        code = _optional(row.get("league_code"))
        if code:
            league_season.setdefault(code, _optional(row.get("season_code")))
    for code, league in existing_leagues.items():
        # #283: League.season_id dropped; a League's Season participation is now
        # a LeagueSeason row. This map is single-valued per League (all the old
        # model ever allowed, and the competition sheet still ties a league_code
        # to one season_code), so record each Season this League plays in.
        for ls in store.league_seasons_for_league(league.id):
            league_season.setdefault(code, season_ref_by_id.get(ls.season_id))

    division_league = {}
    for row in rows["competition"]:
        code = _optional(row.get("division_code"))
        if code:
            division_league.setdefault(code, _optional(row.get("league_code")))
    for code, div in existing_divisions.items():
        # #283: Division.league_id dropped; resolve the owning League via the
        # LeagueSeason the Division hangs off.
        div_ls = store.get_league_season(div.league_season_id)
        division_league.setdefault(
            code, league_ref_by_id.get(div_ls.league_id) if div_ls else None)

    team_program = {}
    for row in rows["permanent_teams"]:
        code = _optional(row.get("team_code"))
        if code:
            team_program.setdefault(code, _optional(row.get("program_code")))
    for code, team in existing_teams.items():
        team_program.setdefault(code, program_ref_by_id.get(team.program_id))

    known_season_codes = {_clean(r.get("season_code"))
                          for r in rows["competition"]
                          if not _blank(r.get("season_code"))} | set(existing_seasons)
    known_league_codes = {_clean(r.get("league_code"))
                          for r in league_rows} | set(existing_leagues)
    known_division_codes = {_clean(r.get("division_code"))
                            for r in rows["competition"]
                            if not _blank(r.get("division_code"))} | set(existing_divisions)
    known_team_codes = upload_team_codes | set(existing_teams)

    seen_registrations = set()
    for index, row in enumerate(rows["registrations"], start=1):
        season_code = _optional(row.get("season_code"))
        team_code = _optional(row.get("team_code"))
        league_code = _optional(row.get("league_code"))
        division_code = _optional(row.get("division_code"))
        if season_code and season_code not in known_season_codes:
            report.error("registrations", index, "unknown_season_code",
                         f"Unknown season_code {season_code}.", "season_code")
        if team_code and team_code not in known_team_codes:
            report.error("registrations", index, "unknown_team_code",
                         f"Unknown team_code {team_code}.", "team_code")
        if league_code and league_code not in known_league_codes:
            report.error("registrations", index, "unknown_league_code",
                         f"Unknown league_code {league_code}.", "league_code")
        if division_code and division_code not in known_division_codes:
            report.error("registrations", index, "unknown_division_code",
                         f"Unknown division_code {division_code}.",
                         "division_code")
        # Deeper chain consistency only once every referenced code resolves
        # (unknown codes are already reported above; this avoids
        # double-reporting the same row).
        if (season_code in known_season_codes
                and team_code in known_team_codes
                and league_code in known_league_codes):
            # The League must resolve to EXACTLY this Season — #260 review
            # decision 2: never inferred, always validated.
            if league_season.get(league_code) != season_code:
                report.error(
                    "registrations", index, "league_season_mismatch",
                    f"League {league_code} does not belong to Season "
                    f"{season_code}.", "league_code")
            # The Team and Season must resolve to the SAME Program.
            tp = team_program.get(team_code)
            sp = season_program.get(season_code)
            if not tp or not sp or tp != sp:
                report.error(
                    "registrations", index, "team_program_mismatch",
                    f"team_code {team_code} (program {tp or 'none'}) cannot "
                    f"register in season {season_code} (program "
                    f"{sp or 'none'}).", "team_code")
            if division_code and division_code in known_division_codes:
                if division_league.get(division_code) != league_code:
                    report.error(
                        "registrations", index, "division_league_mismatch",
                        f"Division {division_code} does not belong to "
                        f"League {league_code}.", "division_code")
        # One registration per team per season.
        if season_code and team_code:
            key = (season_code, team_code)
            if key in seen_registrations:
                report.error(
                    "registrations", index, "duplicate_registration",
                    f"Duplicate registration for team {team_code} in season "
                    f"{season_code}.", "team_code")
            seen_registrations.add(key)

    # -- season venue access (#260, new) -------------------------------------
    sva_pair_active = {}
    for index, row in enumerate(rows["season_venue_access"], start=1):
        season_code = _optional(row.get("season_code"))
        venue_code = _optional(row.get("venue_code"))
        if season_code and season_code not in known_season_codes:
            report.error("season_venue_access", index, "unknown_season_code",
                         f"Unknown season_code {season_code}.", "season_code")
        # venue_code's own existence check runs in the loop below, once the
        # full known-venue-codes set (upload + existing) is built.
        if season_code and venue_code:
            want_active = _bool(row.get("active"), True)
            key = (season_code, venue_code)
            if key in sva_pair_active and sva_pair_active[key] != want_active:
                report.error(
                    "season_venue_access", index, "inconsistent_fields",
                    f"Rows for season {season_code} / venue {venue_code} "
                    "disagree on active.", "active")
            else:
                sva_pair_active[key] = want_active

    known_venue_codes = ({_clean(r.get("venue_code"))
                          for r in rows["venues_rinks"]
                          if not _blank(r.get("venue_code"))}
                         | set(existing_venues))
    for index, row in enumerate(rows["season_venue_access"], start=1):
        venue_code = _optional(row.get("venue_code"))
        if venue_code and venue_code not in known_venue_codes:
            report.error("season_venue_access", index, "unknown_venue_code",
                         f"Unknown venue_code {venue_code}.", "venue_code")
        # Revoking (active=false) a pair that has never existed is a no-op,
        # not an error — flagged as a warning so the operator knows the row
        # had nothing to do (#260 review decision).
        season_code = _optional(row.get("season_code"))
        if (season_code in existing_seasons and venue_code in existing_venues
                and not _bool(row.get("active"), True)):
            existing_pair = store.season_venue_access_for_pair(
                existing_seasons[season_code].id,
                existing_venues[venue_code].id)
            if existing_pair is None or not existing_pair.active:
                report.warning(
                    "season_venue_access", index, "revoke_noop",
                    f"season_code {season_code} / venue_code {venue_code} "
                    "has no active access to revoke; this row is a no-op.",
                    "active")

    entity_summary = {
        "organizations": len(rows["organizations"]),
        "programs": len(rows["programs"]),
        "venues": len(_group_first(rows["venues_rinks"], "venue_code")),
        "rinks": len(rows["venues_rinks"]),
        "seasons": len(_group_first(rows["competition"], "season_code")),
        "leagues": len(_group_first(league_rows, "league_code")),
        "divisions": len(rows["competition"]),
        "clubs": len(rows["clubs"]),
        "permanent_teams": len(_group_first(rows["permanent_teams"], "team_code")),
        "players": len(rows["players"]),
        "registrations": len(rows["registrations"]),
        "season_venue_access": len(rows["season_venue_access"]),
    }
    return {
        "ok": not report.errors,
        "import_type": "hierarchy",
        "summary": {name: len(rows[name]) for name in HIERARCHY_SHEET_NAMES},
        "entities": entity_summary,
        "errors": report.errors,
        "warnings": report.warnings,
    }


def _new_counts() -> dict:
    return {name: {"created": 0, "updated": 0, "skipped": 0}
            for name in (
                "organizations", "programs", "venues", "rinks", "seasons",
                "leagues", "divisions", "clubs", "permanent_teams",
                "players", "registrations", "season_venue_access")}


def _preflight_reassignment_safety(store, rows) -> List[dict]:
    """Reject unsafe program/league/division/venue-access *moves* before any
    write (#214 review, extended #260 review).

    Direct upsert would otherwise bypass the guards the interactive service
    enforces: moving a registration's league or division can strand
    committed games, moving a Team's or Season's program can leave
    registrations cross-program, and revoking a Season's venue access can
    strand a Game whose ice slot lives there. Compares by external code so a
    target created in this same batch is handled without needing its id yet.
    Returns a list of structured errors (empty when every move is safe); the
    caller aborts the whole batch with zero writes when it is non-empty.
    """
    errors: List[dict] = []
    teams = {t.external_ref: t for t in store.all_teams() if t.external_ref}
    seasons = {s.external_ref: s for s in store.all_seasons() if s.external_ref}
    divisions = {d.external_ref: d for d in store.all_divisions()
                 if d.external_ref}
    venues = {v.external_ref: v for v in store.all_venues() if v.external_ref}
    program_code_by_id = {p.id: p.external_ref for p in store.all_programs()
                          if p.external_ref}
    season_code_by_id = {s.id: s.external_ref for s in store.all_seasons()
                         if s.external_ref}
    league_code_by_id = {lg.id: lg.external_ref for lg in store.all_leagues()
                         if lg.external_ref}
    division_code_by_id = {d.id: d.external_ref for d in store.all_divisions()
                           if d.external_ref}
    all_regs = store.all_season_team_registrations()
    all_games = store.all_games()

    # (a) A team's PROGRAM move must keep EVERY retained registration in that
    #     program — active AND inactive/historical, since an inactive
    #     prior-season row would become cross-program on any later
    #     reactivation (#214, renamed #260). Placement is never rewritten.
    for index, row in enumerate(
            _group_first(rows["permanent_teams"], "team_code").values(), start=1):
        code = _clean(row.get("team_code"))
        team = teams.get(code)
        if team is None:
            continue  # a new team has no history to strand
        new_program_code = _clean(row.get("program_code"))
        if new_program_code == program_code_by_id.get(team.program_id):
            continue  # program unchanged
        stranded_regs = []
        for reg in all_regs:
            if reg.team_id != team.id:
                continue
            # #283: registration.season_id dropped; resolve Season via LeagueSeason.
            reg_ls = store.get_league_season(reg.league_season_id)
            season = store.get_season(reg_ls.season_id) if reg_ls else None
            reg_program = (program_code_by_id.get(season.program_id)
                          if season is not None else None)
            if reg_program != new_program_code:
                stranded_regs.append(reg.id)
        if stranded_regs:
            errors.append({
                "sheet": "permanent_teams", "row": index, "field": "program_code",
                "code": "team_program_move_strands_history",
                "message": (f"Moving team {code} to program {new_program_code} "
                            f"would leave {len(stranded_regs)} registration(s) in "
                            "another program; move or clear that history first."),
                "team_code": code,
                "affected_registration_ids": stranded_regs})

    # (b) A registration's LEAGUE or DIVISION move must not strand committed
    #     games (#260 review: league_code is now an explicit, changeable
    #     registration field, not just division_code).
    for index, row in enumerate(rows["registrations"], start=1):
        season = seasons.get(_clean(row.get("season_code")))
        team = teams.get(_clean(row.get("team_code")))
        if season is None or team is None:
            continue  # a new season/team has no existing registration to move
        # #283: registration_for_team_in_season is gone; a Team registers per
        # LeagueSeason, so find its row among the Season's registrations.
        reg = next((r for r in store.registrations_for_season(season.id)
                    if r.team_id == team.id), None)
        if reg is None:
            continue  # a genuinely new registration moves nothing
        new_league_code = _clean(row.get("league_code"))
        new_division_code = _clean(row.get("division_code"))
        # #283: registration.league_id dropped; resolve current League via LeagueSeason.
        reg_ls = store.get_league_season(reg.league_season_id)
        reg_league_id = reg_ls.league_id if reg_ls else None
        league_changed = new_league_code != league_code_by_id.get(reg_league_id)
        division_changed = (
            new_division_code != division_code_by_id.get(reg.division_id))
        if not league_changed and not division_changed:
            continue
        stranded = [g.id for g in all_games
                    if g.season_id == season.id and not g.cancelled
                    and not getattr(g, "is_draft", False)
                    and team.id in (g.home_team_id, g.away_team_id)]
        if stranded and league_changed:
            errors.append({
                "sheet": "registrations", "row": index, "field": "league_code",
                "code": "registration_league_move_strands_games",
                "message": (f"Moving team {_clean(row.get('team_code'))} to "
                            f"league {new_league_code} in season "
                            f"{_clean(row.get('season_code'))} would strand "
                            f"{len(stranded)} scheduled game(s); resolve them "
                            "first."),
                "affected_game_ids": stranded})
        if stranded and division_changed:
            errors.append({
                "sheet": "registrations", "row": index, "field": "division_code",
                "code": "registration_division_move_strands_games",
                "message": (f"Moving team {_clean(row.get('team_code'))} to "
                            f"division {new_division_code} in season "
                            f"{_clean(row.get('season_code'))} would strand "
                            f"{len(stranded)} scheduled game(s); resolve them "
                            "first."),
                "affected_game_ids": stranded})

    # (c) A season's PROGRAM move must keep EVERY retained registration in
    #     that program (#214, renamed #260) — all registrations, active AND
    #     inactive/historical.
    for index, row in enumerate(
            _group_first(rows["competition"], "season_code").values(), start=1):
        code = _clean(row.get("season_code"))
        season = seasons.get(code)
        if season is None:
            continue
        new_program_code = _clean(row.get("program_code"))
        if new_program_code == program_code_by_id.get(season.program_id):
            continue  # program unchanged
        # #283: registration.season_id dropped; resolve each row's Season via
        # its LeagueSeason.
        affected_regs = [
            r.id for r in all_regs
            if (rls := store.get_league_season(r.league_season_id)) is not None
            and rls.season_id == season.id]
        if affected_regs:
            errors.append({
                "sheet": "competition", "row": index, "field": "program_code",
                "code": "season_program_move_strands_history",
                "message": (f"Moving season {code} to program {new_program_code} "
                            f"would leave {len(affected_regs)} registration(s) "
                            "in another program; migrate that history first."),
                "season_code": code,
                "affected_registration_ids": affected_regs})

    # (d) A division's SEASON move must preserve ALL retained history (#214):
    #     all registrations assigned to it (active AND inactive) and all
    #     persisted games in it (including cancelled history, whose
    #     season_id/division_id would otherwise disagree).
    for index, row in enumerate(rows["competition"], start=1):
        code = _clean(row.get("division_code"))
        division = divisions.get(code)
        if division is None:
            continue
        new_season_code = _clean(row.get("season_code"))
        # #283: Division.season_id dropped; resolve current Season via LeagueSeason.
        div_ls = store.get_league_season(division.league_season_id)
        current_season_code = (season_code_by_id.get(div_ls.season_id)
                               if div_ls else None)
        if new_season_code == current_season_code:
            continue  # season unchanged
        affected_regs = [r.id for r in all_regs if r.division_id == division.id]
        affected_games = [g.id for g in all_games if g.division_id == division.id]
        if affected_regs or affected_games:
            errors.append({
                "sheet": "competition", "row": index, "field": "season_code",
                "code": "division_season_move_strands_dependents",
                "message": (f"Moving division {code} to season {new_season_code} "
                            f"would strand {len(affected_regs)} registration(s) "
                            f"and {len(affected_games)} game(s) outside their "
                            "season; migrate that history first."),
                "division_code": code,
                "affected_registration_ids": affected_regs,
                "affected_game_ids": affected_games})

    # (e) Revoking a Season's venue access via reimport must not strand a
    #     Game whose ice slot's rink lives at that venue (#260, new — mirrors
    #     delete_season_venue_access's identical guard).
    for index, row in enumerate(rows["season_venue_access"], start=1):
        season = seasons.get(_clean(row.get("season_code")))
        venue = venues.get(_clean(row.get("venue_code")))
        if season is None or venue is None:
            continue  # nothing existing to revoke
        if _bool(row.get("active"), True):
            continue  # granting/reactivating never strands anything
        existing_pair = store.season_venue_access_for_pair(season.id, venue.id)
        if existing_pair is None or not existing_pair.active:
            continue  # nothing to revoke (a no-op warning, not an error)
        rink_ids = {r.id for r in store.all_rinks() if r.venue_id == venue.id}
        games = [g.id for g in all_games
                if g.season_id == season.id and g.ice_slot_id
                and (slot := store.get_ice_slot(g.ice_slot_id)) is not None
                and slot.rink_id in rink_ids]
        if games:
            errors.append({
                "sheet": "season_venue_access", "row": index, "field": "active",
                "code": "season_venue_access_revoke_strands_games",
                "message": (f"Revoking venue {_clean(row.get('venue_code'))}'s "
                            f"access for season {_clean(row.get('season_code'))} "
                            f"would strand {len(games)} scheduled game(s); "
                            "resolve them first."),
                "affected_game_ids": games})
    return errors


def commit_hierarchy_import(setup, sheets: Dict[str, List[dict]],
                            actor_id: Optional[str] = None) -> dict:
    """Revalidate and atomically upsert the complete hierarchy batch.

    Validation and the reassignment preflight run INSIDE the transaction, before
    the first write, so under the threaded server no concurrent request can
    create a game or registration between the safety check and the write (#214
    review) — the whole check-then-write is one atomic critical section held by
    the store's transaction lock.

    Every actual write goes through a ``setup.upsert_imported_*`` helper
    (#260 review decision 3) — this function only resolves codes to objects
    (read-only lookups built once per sheet, refreshed as each sheet's
    upserts return their objects) and tallies created/updated/skipped
    counts from what each helper reports back. The single outer
    ``store.transaction()`` here is what makes the whole nine-sheet batch
    one atomic commit/rollback unit; the helpers nest inside it because
    ``store.transaction()`` is reentrant.
    """
    store = setup.store
    rows = {name: list((sheets or {}).get(name) or [])
            for name in HIERARCHY_SHEET_NAMES}
    counts = _new_counts()

    with store.transaction():
        result = validate_hierarchy_import(sheets, store)
        if not result["ok"]:
            return {
                "committed": False,
                "import_type": "hierarchy",
                "summary": _new_counts(),
                "errors": result["errors"],
                "warnings": result["warnings"],
            }
        # Reassignment safety (#214 review, extended #260): a program/
        # league/division move or a venue-access revoke that would strand
        # games or orphan registrations aborts the whole batch with zero
        # writes.
        unsafe = _preflight_reassignment_safety(store, rows)
        if unsafe:
            return {
                "committed": False,
                "import_type": "hierarchy",
                "summary": _new_counts(),
                "errors": unsafe,
                "warnings": result["warnings"],
            }

        batch_id = store.next_id("importbatch")

        orgs = {o.external_ref: o for o in store.all_organizations()
                if o.external_ref}
        programs = {o.external_ref: o for o in store.all_programs()
                   if o.external_ref}
        venues = {o.external_ref: o for o in store.all_venues()
                  if o.external_ref}
        rinks = {o.external_ref: o for o in store.all_rinks()
                 if o.external_ref}
        seasons = {o.external_ref: o for o in store.all_seasons()
                   if o.external_ref}
        leagues = {o.external_ref: o for o in store.all_leagues()
                  if o.external_ref}
        divisions = {o.external_ref: o for o in store.all_divisions()
                     if o.external_ref}
        clubs = {o.external_ref: o for o in store.all_clubs()
                 if o.external_ref}
        teams = {o.external_ref: o for o in store.all_teams()
                 if o.external_ref}
        players = {o.external_ref: o for o in store.all_players()
                  if o.external_ref}

        def _tally(bucket, created, changed):
            if created:
                counts[bucket]["created"] += 1
            elif changed:
                counts[bucket]["updated"] += 1
            else:
                counts[bucket]["skipped"] += 1

        for row in rows["organizations"]:
            code = _clean(row.get("organization_code"))
            obj, created, changed = setup.upsert_imported_organization(
                code, _clean(row.get("organization_name")),
                _clean(row.get("short_name")), existing=orgs.get(code),
                actor_id=actor_id, import_batch_id=batch_id)
            orgs[code] = obj
            _tally("organizations", created, changed)

        for row in rows["programs"]:
            code = _clean(row.get("program_code"))
            org_code = _optional(row.get("operator_organization_code"))
            org = orgs.get(org_code) if org_code else None
            obj, created, changed = setup.upsert_imported_program(
                code, _clean(row.get("program_name")),
                _clean(row.get("country")),
                _clean(row.get("timezone"), "UTC") or "UTC",
                org.id if org else None, existing=programs.get(code),
                actor_id=actor_id, import_batch_id=batch_id)
            programs[code] = obj
            _tally("programs", created, changed)

        # Venues carry no program/league column at all (#233 Slice E, #260)
        # — the legacy Venue.league_id bridge is never written here.
        for code, row in _group_first(
                rows["venues_rinks"], "venue_code").items():
            org = orgs[_clean(row.get("organization_code"))]
            obj, created, changed = setup.upsert_imported_venue(
                code, _clean(row.get("venue_name")),
                _clean(row.get("address")),
                _clean(row.get("timezone"), "UTC") or "UTC", org.id,
                existing=venues.get(code), actor_id=actor_id,
                import_batch_id=batch_id)
            venues[code] = obj
            _tally("venues", created, changed)

        for row in rows["venues_rinks"]:
            code = _clean(row.get("rink_code"))
            venue = venues[_clean(row.get("venue_code"))]
            obj, created, changed = setup.upsert_imported_rink(
                code, _clean(row.get("rink_name")), venue.id,
                existing=rinks.get(code), actor_id=actor_id,
                import_batch_id=batch_id)
            rinks[code] = obj
            _tally("rinks", created, changed)

        for code, row in _group_first(
                rows["competition"], "season_code").items():
            program = programs[_clean(row.get("program_code"))]
            obj, created, changed = setup.upsert_imported_season(
                code, _clean(row.get("season_name")), program.id,
                existing=seasons.get(code), actor_id=actor_id,
                import_batch_id=batch_id)
            seasons[code] = obj
            _tally("seasons", created, changed)

        # League (season-scoped, was "level") — REQUIRED on every competition
        # row (#260 review), unlike the old optional level_code/level_name.
        for code, row in _group_first(
                rows["competition"], "league_code").items():
            season = seasons[_clean(row.get("season_code"))]
            obj, created, changed = setup.upsert_imported_league(
                code, _clean(row.get("league_name")),
                _int(row.get("league_sort_order"), 0), season.id,
                existing=leagues.get(code), actor_id=actor_id,
                import_batch_id=batch_id)
            leagues[code] = obj
            _tally("leagues", created, changed)

        for row in rows["competition"]:
            code = _optional(row.get("division_code"))
            if not code:
                continue  # optional per row (#260) — no division on this row
            season = seasons[_clean(row.get("season_code"))]
            league = leagues[_clean(row.get("league_code"))]
            obj, created, changed = setup.upsert_imported_division(
                code, _clean(row.get("division_name")),
                _clean(row.get("age_group")), season.id, league.id,
                existing=divisions.get(code), actor_id=actor_id,
                import_batch_id=batch_id)
            divisions[code] = obj
            _tally("divisions", created, changed)

        # Clubs (#260): matched by club_code, never by name — a renamed Club
        # (same code, new club_name) updates the existing record in place.
        for row in rows["clubs"]:
            code = _clean(row.get("club_code"))
            obj, created, changed = setup.upsert_imported_club(
                code, _clean(row.get("club_name")),
                _clean(row.get("country")), existing=clubs.get(code),
                actor_id=actor_id, import_batch_id=batch_id)
            clubs[code] = obj
            _tally("clubs", created, changed)

        # Permanent Program teams (#180, extended #260 with optional
        # club_code): blank/NA club_code means club_id=None, on both a
        # create AND a repeat row (#260 review decision 1) — a placeholder
        # Club is never created, but a blank cell on a repeat import
        # genuinely unassigns a previously-set club, exactly like the
        # interactive "— none —" option already does.
        for code, row in _group_first(
                rows["permanent_teams"], "team_code").items():
            program = programs[_clean(row.get("program_code"))]
            club_code = _optional(row.get("club_code"))
            club = clubs.get(club_code) if club_code else None
            obj, created, changed = setup.upsert_imported_team(
                code, _clean(row.get("team_name")), program.id,
                club.id if club else None, existing=teams.get(code),
                actor_id=actor_id, import_batch_id=batch_id)
            teams[code] = obj
            _tally("permanent_teams", created, changed)

        # Players (#260, new): matched by player_code, never by name. Email
        # sync mirrors add_player's own behavior exactly (setup_service.py)
        # — an existing player:<id> ContactDestination's value is updated in
        # place, never duplicated; omitting the email column on a repeat row
        # leaves a previously-set contact untouched.
        for row in rows["players"]:
            code = _clean(row.get("player_code"))
            team = teams[_clean(row.get("team_code"))]
            jersey = _optional(row.get("jersey_number"))
            name = (f"{_clean(row.get('first_name'))} "
                    f"{_clean(row.get('last_name'))}").strip()
            obj, created, changed = setup.upsert_imported_player(
                code, name, team.id,
                Position(_clean(row.get("position")).lower()),
                int(jersey) if jersey else None, _optional(row.get("email")),
                existing=players.get(code), actor_id=actor_id,
                import_batch_id=batch_id)
            players[code] = obj
            _tally("players", created, changed)

        # Season registrations (#180, #260): one per (season, team),
        # assigning an explicit league (#260 review decision 2 — never
        # derived) and optional division. SeasonTeamRegistration has no
        # external code of its own — the (season_id, team_id) pair is its
        # identity, so a repeat import updates the league/division in place
        # rather than duplicating.
        for row in rows["registrations"]:
            season = seasons[_clean(row.get("season_code"))]
            team = teams[_clean(row.get("team_code"))]
            league = leagues[_clean(row.get("league_code"))]
            division_code = _optional(row.get("division_code"))
            division = divisions.get(division_code) if division_code else None
            reg, created, changed = setup.upsert_imported_registration(
                season.id, team.id, league.id,
                division.id if division else None, actor_id=actor_id,
                import_batch_id=batch_id)
            _tally("registrations", created, changed)

        # Season venue access (#260, new): grants, reactivates, or revokes a
        # Season's access to a Venue's ice — never Venue.league_id. Blank
        # active defaults to true. Revoking a pair that never existed is a
        # no-op (already flagged as a warning by validate), not a write.
        for row in rows["season_venue_access"]:
            season = seasons[_clean(row.get("season_code"))]
            venue = venues[_clean(row.get("venue_code"))]
            want_active = _bool(row.get("active"), True)
            access, created, changed = setup.upsert_imported_venue_access(
                season.id, venue.id, want_active, actor_id=actor_id,
                import_batch_id=batch_id)
            if access is None:
                counts["season_venue_access"]["skipped"] += 1  # no-op revoke
            else:
                _tally("season_venue_access", created, changed)

        totals = {
            key: sum(values[key] for values in counts.values())
            for key in ("created", "updated", "skipped")}
        setup._audit(
            "import_committed", "import_batch", batch_id, actor_id,
            {"import_type": "hierarchy", "errors": 0,
             **totals, "summary": counts})

    return {
        "committed": True,
        "import_type": "hierarchy",
        "summary": counts,
        "warnings": result["warnings"],
        "errors": [],
    }
