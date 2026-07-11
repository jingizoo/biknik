"""Idempotent client hierarchy CSV import (#174 PR E2).

Four CSV sheets describe the durable setup hierarchy before teams and players:
organizations, leagues, venues+rinks, and competition structure. Validation is
read-only and resolves references against both the same upload and existing
external_ref values. Commit re-runs validation and applies the whole batch in a
single store transaction; missing rows are never treated as deletes.
"""

from collections import OrderedDict
from typing import Dict, Iterable, List, Optional, Tuple

from ..domain import (
    Division, League, Level, Organization, Rink, Season,
    SeasonTeamRegistration, Team, Venue)


HIERARCHY_SHEET_NAMES = (
    "organizations", "leagues", "venues_rinks", "competition",
    "permanent_teams", "registrations")
HIERARCHY_CSV_KEYS = tuple(f"{name}_csv" for name in HIERARCHY_SHEET_NAMES)

HIERARCHY_TEMPLATES = {
    "organizations_csv": (
        "organization_code,organization_name,short_name\n"
        "CANLON,Canlon Ice Facilities,Canlon\n"
    ),
    "leagues_csv": (
        "league_code,organization_code,league_name,country,timezone\n"
        "OVER55,CANLON,Over 55,US,America/Chicago\n"
    ),
    "venues_rinks_csv": (
        "venue_code,organization_code,league_code,venue_name,address,timezone,rink_code,rink_name\n"
        "PLAINFIELD,CANLON,OVER55,Plainfield Ice,123 Main St,America/Chicago,PF1,Rink 1\n"
    ),
    "competition_csv": (
        "league_code,season_code,season_name,level_code,level_name,level_sort_order,division_code,division_name,age_group\n"
        "OVER55,FALL26,Fall 2026,L1,Level 1,1,DIVA,Division A,Adult\n"
    ),
    # Permanent league teams (#180): a team is a permanent member of a league,
    # keyed by team_code; it carries no division here — season participation is
    # a separate registration sheet below. Club association is deferred from
    # this slice (#214 review): Club has no stable external code yet, so it is
    # left to a dedicated Club-identity slice.
    "permanent_teams_csv": (
        "league_code,team_code,team_name\n"
        "OVER55,LIONS,Lions\n"
    ),
    # Season registrations (#180): one row per team's participation in a season,
    # keyed by (season_code, team_code), assigning that season's division.
    "registrations_csv": (
        "season_code,team_code,division_code\n"
        "FALL26,LIONS,DIVA\n"
    ),
}

_REQUIRED = {
    "organizations": ("organization_code", "organization_name"),
    "leagues": ("league_code", "organization_code", "league_name"),
    "venues_rinks": (
        "venue_code", "organization_code", "league_code", "venue_name",
        "rink_code", "rink_name"),
    "competition": (
        "league_code", "season_code", "season_name", "division_code",
        "division_name"),
    "permanent_teams": ("league_code", "team_code", "team_name"),
    "registrations": ("season_code", "team_code", "division_code"),
}


def _blank(value) -> bool:
    return value is None or not str(value).strip()


def _clean(value, default="") -> str:
    return default if _blank(value) else str(value).strip()


def _optional(value) -> Optional[str]:
    return None if _blank(value) else str(value).strip()


def _int(value, default=0) -> int:
    return default if _blank(value) else int(str(value).strip())


class _Report:
    def __init__(self):
        self.errors: List[dict] = []
        self.warnings: List[dict] = []

    def error(self, sheet: str, row: int, message: str,
              field: Optional[str] = None) -> None:
        item = {"sheet": sheet, "row": row, "message": message}
        if field:
            item["field"] = field
        self.errors.append(item)

    def warning(self, sheet: str, row: int, message: str) -> None:
        self.warnings.append({"sheet": sheet, "row": row, "message": message})


def _existing_map(report: _Report, rows: Iterable, entity: str) -> dict:
    out = {}
    for obj in rows:
        code = _optional(getattr(obj, "external_ref", None))
        if not code:
            continue
        if code in out:
            report.error(
                "existing_data", 0,
                f"Multiple existing {entity} records use external code {code}.",
                field=f"{entity}_code")
        else:
            out[code] = obj
    return out


def _check_required(report: _Report, sheet: str, rows: List[dict]) -> None:
    for index, row in enumerate(rows, start=1):
        for field in _REQUIRED[sheet]:
            if _blank(row.get(field)):
                report.error(sheet, index, f"{field} is required.", field)


def _check_unique(report: _Report, sheet: str, rows: List[dict],
                  field: str) -> None:
    seen = set()
    for index, row in enumerate(rows, start=1):
        code = _optional(row.get(field))
        if not code:
            continue
        if code in seen:
            report.error(sheet, index, f"Duplicate {field} {code}.", field)
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
                sheet, index,
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
    already-persisted ``external_ref``. The function only reads the store.
    """
    rows = {name: list((sheets or {}).get(name) or [])
            for name in HIERARCHY_SHEET_NAMES}
    report = _Report()
    if not any(rows.values()):
        report.error("hierarchy", 0, "At least one hierarchy CSV row is required.")

    for name in HIERARCHY_SHEET_NAMES:
        _check_required(report, name, rows[name])

    _check_unique(report, "organizations", rows["organizations"],
                  "organization_code")
    _check_unique(report, "leagues", rows["leagues"], "league_code")
    _check_unique(report, "venues_rinks", rows["venues_rinks"], "rink_code")
    _check_unique(report, "competition", rows["competition"], "division_code")
    _check_unique(report, "permanent_teams", rows["permanent_teams"], "team_code")

    _consistent_groups(
        report, "venues_rinks", rows["venues_rinks"], "venue_code",
        ("organization_code", "league_code", "venue_name", "address", "timezone"))
    _consistent_groups(
        report, "competition", rows["competition"], "season_code",
        ("league_code", "season_name"))
    level_rows = [row for row in rows["competition"]
                  if not _blank(row.get("level_code"))]
    _consistent_groups(
        report, "competition", level_rows, "level_code",
        ("season_code", "level_name", "level_sort_order"))
    _consistent_groups(
        report, "permanent_teams", rows["permanent_teams"], "team_code",
        ("league_code", "team_name"))

    existing_orgs = _existing_map(
        report, store.all_organizations(), "organization")
    existing_leagues = _existing_map(report, store.all_leagues(), "league")
    _existing_map(report, store.all_venues(), "venue")
    _existing_map(report, store.all_rinks(), "rink")
    existing_seasons = _existing_map(report, store.all_seasons(), "season")
    _existing_map(report, store.all_levels(), "level")
    existing_divisions = _existing_map(report, store.all_divisions(), "division")
    existing_teams = _existing_map(report, store.all_teams(), "team")

    upload_org_codes = {
        _clean(row.get("organization_code")) for row in rows["organizations"]
        if not _blank(row.get("organization_code"))}
    known_org_codes = upload_org_codes | set(existing_orgs)

    upload_league_owner = {}
    for index, row in enumerate(rows["leagues"], start=1):
        code = _optional(row.get("league_code"))
        org_code = _optional(row.get("organization_code"))
        if org_code and org_code not in known_org_codes:
            report.error("leagues", index,
                         f"Unknown organization_code {org_code}.",
                         "organization_code")
        if code:
            upload_league_owner[code] = org_code

    existing_league_owner = {}
    existing_org_by_id = {obj.id: obj for obj in store.all_organizations()}
    for code, league in existing_leagues.items():
        owner = existing_org_by_id.get(league.organization_id)
        existing_league_owner[code] = (
            _optional(owner.external_ref) if owner is not None else None)

    known_league_codes = set(existing_leagues) | set(upload_league_owner)
    league_owner = dict(existing_league_owner)
    league_owner.update(upload_league_owner)

    for index, row in enumerate(rows["venues_rinks"], start=1):
        org_code = _optional(row.get("organization_code"))
        league_code = _optional(row.get("league_code"))
        if org_code and org_code not in known_org_codes:
            report.error("venues_rinks", index,
                         f"Unknown organization_code {org_code}.",
                         "organization_code")
        if league_code and league_code not in known_league_codes:
            report.error("venues_rinks", index,
                         f"Unknown league_code {league_code}.", "league_code")
        expected_owner = league_owner.get(league_code)
        if league_code in known_league_codes and expected_owner is None:
            report.error(
                "venues_rinks", index,
                f"League {league_code} has no resolvable organization_code; "
                "include its league row to repair ownership.", "league_code")
        elif expected_owner and org_code and expected_owner != org_code:
            report.error(
                "venues_rinks", index,
                f"organization_code {org_code} does not own league_code "
                f"{league_code} (expected {expected_owner}).",
                "organization_code")

    for index, row in enumerate(rows["competition"], start=1):
        league_code = _optional(row.get("league_code"))
        if league_code and league_code not in known_league_codes:
            report.error("competition", index,
                         f"Unknown league_code {league_code}.", "league_code")
        level_code = _optional(row.get("level_code"))
        level_name = _optional(row.get("level_name"))
        if bool(level_code) != bool(level_name):
            report.error(
                "competition", index,
                "level_code and level_name must be supplied together.",
                "level_code" if not level_code else "level_name")
        if not _blank(row.get("level_sort_order")):
            try:
                int(_clean(row.get("level_sort_order")))
            except ValueError:
                report.error(
                    "competition", index,
                    f"Invalid level_sort_order {row.get('level_sort_order')!r}.",
                    "level_sort_order")

    # -- permanent teams + season registrations (#180) ---------------------
    # Codes may resolve from this upload or from existing external_refs. Build
    # the league of each season/team and the season of each division so a
    # registration can be checked for division-in-season and (the #200 guard)
    # team-league == season-league before any write.
    league_ref_by_id = {lg.id: code for code, lg in existing_leagues.items()}
    season_ref_by_id = {s.id: code for code, s in existing_seasons.items()}

    season_league = {}
    for row in rows["competition"]:
        code = _optional(row.get("season_code"))
        if code:
            season_league.setdefault(code, _optional(row.get("league_code")))
    for code, season in existing_seasons.items():
        season_league.setdefault(code, league_ref_by_id.get(season.league_id))

    division_season = {}
    for row in rows["competition"]:
        code = _optional(row.get("division_code"))
        if code:
            division_season.setdefault(code, _optional(row.get("season_code")))
    for code, div in existing_divisions.items():
        division_season.setdefault(code, season_ref_by_id.get(div.season_id))

    team_league = {}
    for row in rows["permanent_teams"]:
        code = _optional(row.get("team_code"))
        if code:
            team_league.setdefault(code, _optional(row.get("league_code")))
    for code, team in existing_teams.items():
        team_league.setdefault(code, league_ref_by_id.get(team.league_id))

    known_season_codes = {_clean(r.get("season_code"))
                          for r in rows["competition"]
                          if not _blank(r.get("season_code"))} | set(existing_seasons)
    known_division_codes = {_clean(r.get("division_code"))
                            for r in rows["competition"]
                            if not _blank(r.get("division_code"))} | set(existing_divisions)
    known_team_codes = {_clean(r.get("team_code"))
                        for r in rows["permanent_teams"]
                        if not _blank(r.get("team_code"))} | set(existing_teams)

    for index, row in enumerate(rows["permanent_teams"], start=1):
        league_code = _optional(row.get("league_code"))
        if league_code and league_code not in known_league_codes:
            report.error("permanent_teams", index,
                         f"Unknown league_code {league_code}.", "league_code")

    seen_registrations = set()
    for index, row in enumerate(rows["registrations"], start=1):
        season_code = _optional(row.get("season_code"))
        team_code = _optional(row.get("team_code"))
        division_code = _optional(row.get("division_code"))
        if season_code and season_code not in known_season_codes:
            report.error("registrations", index,
                         f"Unknown season_code {season_code}.", "season_code")
        if team_code and team_code not in known_team_codes:
            report.error("registrations", index,
                         f"Unknown team_code {team_code}.", "team_code")
        if division_code and division_code not in known_division_codes:
            report.error("registrations", index,
                         f"Unknown division_code {division_code}.", "division_code")
        # Deeper consistency only once the three codes resolve (unknown codes
        # are already reported above, so this avoids double-reporting).
        if (season_code in known_season_codes
                and team_code in known_team_codes
                and division_code in known_division_codes):
            # The division must resolve to EXACTLY this season — a null or
            # different parent season is invalid, not a pass (#214 review).
            if division_season.get(division_code) != season_code:
                report.error(
                    "registrations", index,
                    f"division_code {division_code} does not belong to "
                    f"season_code {season_code}.", "division_code")
            # The team and season must both resolve to the SAME concrete league;
            # a missing league on either side is invalid (#200/#214 review).
            tl = team_league.get(team_code)
            sl = season_league.get(season_code)
            if not tl or not sl or tl != sl:
                report.error(
                    "registrations", index,
                    f"team_code {team_code} (league {tl or 'none'}) cannot "
                    f"register in season {season_code} (league {sl or 'none'}).",
                    "team_code")
        # One registration per team per season.
        if season_code and team_code:
            key = (season_code, team_code)
            if key in seen_registrations:
                report.error(
                    "registrations", index,
                    f"Duplicate registration for team {team_code} in season "
                    f"{season_code}.", "team_code")
            seen_registrations.add(key)

    entity_summary = {
        "organizations": len(rows["organizations"]),
        "leagues": len(rows["leagues"]),
        "venues": len(_group_first(rows["venues_rinks"], "venue_code")),
        "rinks": len(rows["venues_rinks"]),
        "seasons": len(_group_first(rows["competition"], "season_code")),
        "levels": len(_group_first(level_rows, "level_code")),
        "divisions": len(rows["competition"]),
        "permanent_teams": len(_group_first(rows["permanent_teams"], "team_code")),
        "registrations": len(rows["registrations"]),
    }
    return {
        "ok": not report.errors,
        "import_type": "hierarchy",
        "summary": {name: len(rows[name]) for name in HIERARCHY_SHEET_NAMES},
        "entities": entity_summary,
        "errors": report.errors,
        "warnings": report.warnings,
    }


def _apply_changes(obj, values: dict) -> List[str]:
    changed = []
    for field, value in values.items():
        if getattr(obj, field) != value:
            setattr(obj, field, value)
            changed.append(field)
    return changed


def _new_counts() -> dict:
    return {name: {"created": 0, "updated": 0, "skipped": 0}
            for name in (
                "organizations", "leagues", "venues", "rinks", "seasons",
                "levels", "divisions", "permanent_teams", "registrations")}


def _preflight_reassignment_safety(store, rows) -> List[dict]:
    """Reject unsafe league/division *moves* before any write (#214 review).

    Direct upsert would otherwise bypass the guards the interactive service
    enforces: moving a registration's division can strand committed games (see
    ``assign_season_team_division``), and moving a Team's league can leave its
    active registrations cross-league. Compares by external code so a target
    created in this same batch is handled without needing its id yet. Returns a
    list of structured errors (empty when every move is safe); the caller aborts
    the whole batch with zero writes when it is non-empty.
    """
    errors: List[dict] = []
    teams = {t.external_ref: t for t in store.all_teams() if t.external_ref}
    seasons = {s.external_ref: s for s in store.all_seasons() if s.external_ref}
    divisions = {d.external_ref: d for d in store.all_divisions()
                 if d.external_ref}
    league_code_by_id = {lg.id: lg.external_ref for lg in store.all_leagues()
                         if lg.external_ref}
    season_code_by_id = {s.id: s.external_ref for s in store.all_seasons()
                         if s.external_ref}
    division_code_by_id = {d.id: d.external_ref for d in store.all_divisions()
                           if d.external_ref}
    all_regs = store.all_season_team_registrations()
    all_teams = store.all_teams()
    all_games = store.all_games()
    divisions_by_season = {}
    for _division in store.all_divisions():
        divisions_by_season.setdefault(_division.season_id, set()).add(_division.id)

    # (a) A team's league move must keep EVERY retained dependency in that
    #     league. This means all registrations — active AND inactive/historical,
    #     since an inactive prior-season row would become cross-league on any
    #     later reactivation — and the preserved legacy Team.division_id (kept
    #     for legacy game scope), whose division's season must also resolve to
    #     the target league (#214 review). Historical placement is never
    #     silently rewritten.
    for index, row in enumerate(
            _group_first(rows["permanent_teams"], "team_code").values(), start=1):
        code = _clean(row.get("team_code"))
        team = teams.get(code)
        if team is None:
            continue  # a new team has no history to strand
        new_league_code = _clean(row.get("league_code"))
        if new_league_code == league_code_by_id.get(team.league_id):
            continue  # league unchanged
        stranded_regs = []
        for reg in all_regs:
            if reg.team_id != team.id:
                continue
            season = store.get_season(reg.season_id)
            reg_league = (league_code_by_id.get(season.league_id)
                          if season is not None else None)
            if reg_league != new_league_code:
                stranded_regs.append(reg.id)
        stranded_divisions = []
        if team.division_id:
            division = store.get_division(team.division_id)
            div_season = (store.get_season(division.season_id)
                          if division is not None else None)
            div_league = (league_code_by_id.get(div_season.league_id)
                          if div_season is not None else None)
            if div_league != new_league_code:
                stranded_divisions.append(team.division_id)
        if stranded_regs or stranded_divisions:
            errors.append({
                "sheet": "permanent_teams", "row": index, "field": "league_code",
                "message": (f"Moving team {code} to league {new_league_code} "
                            f"would leave {len(stranded_regs)} registration(s) "
                            f"and {len(stranded_divisions)} legacy division(s) in "
                            "another league; move or clear that history first."),
                "reason": "team_league_move_strands_history",
                "team_code": code,
                "affected_registration_ids": stranded_regs,
                "affected_division_ids": stranded_divisions})

    # (b) A registration's division move must not strand committed games.
    for index, row in enumerate(rows["registrations"], start=1):
        season = seasons.get(_clean(row.get("season_code")))
        team = teams.get(_clean(row.get("team_code")))
        if season is None or team is None:
            continue  # a new season/team has no existing registration to move
        reg = store.registration_for_team_in_season(season.id, team.id)
        if reg is None or not reg.active:
            continue  # a new registration moves nothing
        new_division_code = _clean(row.get("division_code"))
        if new_division_code == division_code_by_id.get(reg.division_id):
            continue  # division unchanged
        stranded = [g.id for g in all_games
                    if g.season_id == season.id and not g.cancelled
                    and not getattr(g, "is_draft", False)
                    and team.id in (g.home_team_id, g.away_team_id)]
        if stranded:
            errors.append({
                "sheet": "registrations", "row": index, "field": "division_code",
                "message": (f"Moving team {_clean(row.get('team_code'))} to "
                            f"division {new_division_code} in season "
                            f"{_clean(row.get('season_code'))} would strand "
                            f"{len(stranded)} scheduled game(s); resolve them "
                            "first."),
                "reason": "registration_division_move_strands_games",
                "affected_game_ids": stranded})

    # (c) A season's league move must keep EVERY retained dependency in that
    #     league (#214 review): ALL registrations in the season (active AND
    #     inactive/historical) and every legacy Team whose division_id points to
    #     a division under this season — otherwise the team's permanent league
    #     and its legacy division hierarchy disagree after the move.
    for index, row in enumerate(
            _group_first(rows["competition"], "season_code").values(), start=1):
        code = _clean(row.get("season_code"))
        season = seasons.get(code)
        if season is None:
            continue
        new_league_code = _clean(row.get("league_code"))
        if new_league_code == league_code_by_id.get(season.league_id):
            continue  # league unchanged
        affected_regs = [r.id for r in all_regs if r.season_id == season.id]
        season_division_ids = divisions_by_season.get(season.id, set())
        affected_teams = [t.id for t in all_teams
                          if t.division_id in season_division_ids]
        if affected_regs or affected_teams:
            errors.append({
                "sheet": "competition", "row": index, "field": "league_code",
                "message": (f"Moving season {code} to league {new_league_code} "
                            f"would leave {len(affected_regs)} registration(s) "
                            f"and {len(affected_teams)} legacy team-division "
                            "link(s) in another league; migrate that history "
                            "first."),
                "reason": "season_league_move_strands_history",
                "season_code": code,
                "affected_registration_ids": affected_regs,
                "affected_team_ids": affected_teams})

    # (d) A division's season move must preserve ALL retained history (#214
    #     review): all registrations assigned to it (active AND inactive), all
    #     persisted games in it (INCLUDING cancelled history, whose season_id/
    #     division_id would otherwise disagree), and every legacy Team whose
    #     division_id points to it.
    for index, row in enumerate(rows["competition"], start=1):
        code = _clean(row.get("division_code"))
        division = divisions.get(code)
        if division is None:
            continue
        new_season_code = _clean(row.get("season_code"))
        if new_season_code == season_code_by_id.get(division.season_id):
            continue  # season unchanged
        affected_regs = [r.id for r in all_regs if r.division_id == division.id]
        affected_games = [g.id for g in all_games if g.division_id == division.id]
        affected_teams = [t.id for t in all_teams if t.division_id == division.id]
        if affected_regs or affected_games or affected_teams:
            errors.append({
                "sheet": "competition", "row": index, "field": "season_code",
                "message": (f"Moving division {code} to season {new_season_code} "
                            f"would strand {len(affected_regs)} registration(s), "
                            f"{len(affected_games)} game(s), and "
                            f"{len(affected_teams)} legacy team link(s) outside "
                            "their season; migrate that history first."),
                "reason": "division_season_move_strands_dependents",
                "division_code": code,
                "affected_registration_ids": affected_regs,
                "affected_game_ids": affected_games,
                "affected_team_ids": affected_teams})
    return errors


def commit_hierarchy_import(setup, sheets: Dict[str, List[dict]],
                            actor_id: Optional[str] = None) -> dict:
    """Revalidate and atomically upsert the complete hierarchy batch.

    Validation and the reassignment preflight run INSIDE the transaction, before
    the first write, so under the threaded server no concurrent request can
    create a game or registration between the safety check and the write (#214
    review) — the whole check-then-write is one atomic critical section held by
    the store's transaction lock.
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
        # Reassignment safety (#214 review): a league/division/season move that
        # would strand games or orphan registrations aborts the whole batch with
        # zero writes.
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
        leagues = {o.external_ref: o for o in store.all_leagues()
                   if o.external_ref}
        venues = {o.external_ref: o for o in store.all_venues()
                  if o.external_ref}
        rinks = {o.external_ref: o for o in store.all_rinks()
                 if o.external_ref}
        seasons = {o.external_ref: o for o in store.all_seasons()
                   if o.external_ref}
        levels = {o.external_ref: o for o in store.all_levels()
                  if o.external_ref}
        divisions = {o.external_ref: o for o in store.all_divisions()
                     if o.external_ref}
        teams = {o.external_ref: o for o in store.all_teams()
                 if o.external_ref}

        def audit(action, entity, obj, detail=None):
            payload = {"import_batch_id": batch_id,
                       "external_ref": obj.external_ref}
            payload.update(detail or {})
            setup._audit(action, entity, obj.id, actor_id, payload)

        for row in rows["organizations"]:
            code = _clean(row.get("organization_code"))
            values = {
                "name": _clean(row.get("organization_name")),
                "short_name": _clean(row.get("short_name")),
            }
            obj = orgs.get(code)
            if obj is None:
                obj = Organization(id=store.next_id("org"),
                                   external_ref=code, **values)
                store.add_organization(obj)
                orgs[code] = obj
                counts["organizations"]["created"] += 1
                audit("organization_created", "organization", obj)
            else:
                changed = _apply_changes(obj, values)
                if changed:
                    store.save_organization(obj)
                    counts["organizations"]["updated"] += 1
                    audit("organization_updated", "organization", obj,
                          {"changed_fields": changed})
                else:
                    counts["organizations"]["skipped"] += 1

        for row in rows["leagues"]:
            code = _clean(row.get("league_code"))
            org = orgs[_clean(row.get("organization_code"))]
            values = {
                "name": _clean(row.get("league_name")),
                "country": _clean(row.get("country")),
                "timezone": _clean(row.get("timezone"), "UTC") or "UTC",
                "organization_id": org.id,
            }
            obj = leagues.get(code)
            if obj is None:
                obj = League(id=store.next_id("league"), external_ref=code,
                             **values)
                store.add_league(obj)
                leagues[code] = obj
                counts["leagues"]["created"] += 1
                audit("league_created", "league", obj,
                      {"organization_id": org.id})
            else:
                changed = _apply_changes(obj, values)
                if changed:
                    store.save_league(obj)
                    counts["leagues"]["updated"] += 1
                    audit("league_updated", "league", obj,
                          {"organization_id": org.id,
                           "changed_fields": changed})
                else:
                    counts["leagues"]["skipped"] += 1

        for code, row in _group_first(
                rows["venues_rinks"], "venue_code").items():
            org = orgs[_clean(row.get("organization_code"))]
            league = leagues[_clean(row.get("league_code"))]
            values = {
                "name": _clean(row.get("venue_name")),
                "address": _clean(row.get("address")),
                "timezone": _clean(row.get("timezone"), "UTC") or "UTC",
                "organization_id": org.id,
                "league_id": league.id,
            }
            obj = venues.get(code)
            if obj is None:
                obj = Venue(id=store.next_id("venue"), external_ref=code,
                            **values)
                store.add_venue(obj)
                venues[code] = obj
                counts["venues"]["created"] += 1
                audit("venue_created", "venue", obj,
                      {"organization_id": org.id, "league_id": league.id})
            else:
                changed = _apply_changes(obj, values)
                if changed:
                    store.save_venue(obj)
                    counts["venues"]["updated"] += 1
                    audit("venue_updated", "venue", obj,
                          {"organization_id": org.id, "league_id": league.id,
                           "changed_fields": changed})
                else:
                    counts["venues"]["skipped"] += 1

        for row in rows["venues_rinks"]:
            code = _clean(row.get("rink_code"))
            venue = venues[_clean(row.get("venue_code"))]
            values = {"name": _clean(row.get("rink_name")),
                      "venue_id": venue.id}
            obj = rinks.get(code)
            if obj is None:
                obj = Rink(id=store.next_id("rink"), external_ref=code,
                           **values)
                store.add_rink(obj)
                rinks[code] = obj
                counts["rinks"]["created"] += 1
                audit("rink_created", "rink", obj, {"venue_id": venue.id})
            else:
                changed = _apply_changes(obj, values)
                if changed:
                    store.save_rink(obj)
                    counts["rinks"]["updated"] += 1
                    audit("rink_updated", "rink", obj,
                          {"venue_id": venue.id, "changed_fields": changed})
                else:
                    counts["rinks"]["skipped"] += 1

        for code, row in _group_first(
                rows["competition"], "season_code").items():
            league = leagues[_clean(row.get("league_code"))]
            values = {"league_id": league.id,
                      "name": _clean(row.get("season_name"))}
            obj = seasons.get(code)
            if obj is None:
                obj = Season(id=store.next_id("season"), external_ref=code,
                             **values)
                store.add_season(obj)
                seasons[code] = obj
                counts["seasons"]["created"] += 1
                audit("season_created", "season", obj,
                      {"league_id": league.id})
            else:
                changed = _apply_changes(obj, values)
                if changed:
                    store.save_season(obj)
                    counts["seasons"]["updated"] += 1
                    audit("season_updated", "season", obj,
                          {"league_id": league.id, "changed_fields": changed})
                else:
                    counts["seasons"]["skipped"] += 1

        competition_level_rows = [
            row for row in rows["competition"]
            if not _blank(row.get("level_code"))]
        for code, row in _group_first(
                competition_level_rows, "level_code").items():
            season = seasons[_clean(row.get("season_code"))]
            values = {
                "season_id": season.id,
                "name": _clean(row.get("level_name")),
                "sort_order": _int(row.get("level_sort_order"), 0),
            }
            obj = levels.get(code)
            if obj is None:
                obj = Level(id=store.next_id("level"), external_ref=code,
                            **values)
                store.add_level(obj)
                levels[code] = obj
                counts["levels"]["created"] += 1
                audit("level_created", "level", obj,
                      {"season_id": season.id})
            else:
                changed = _apply_changes(obj, values)
                if changed:
                    store.save_level(obj)
                    counts["levels"]["updated"] += 1
                    audit("level_updated", "level", obj,
                          {"season_id": season.id, "changed_fields": changed})
                else:
                    counts["levels"]["skipped"] += 1

        for row in rows["competition"]:
            code = _clean(row.get("division_code"))
            season = seasons[_clean(row.get("season_code"))]
            level_code = _optional(row.get("level_code"))
            level = levels.get(level_code) if level_code else None
            values = {
                "season_id": season.id,
                "name": _clean(row.get("division_name")),
                "age_group": _clean(row.get("age_group")),
                "level_id": level.id if level else None,
            }
            obj = divisions.get(code)
            if obj is None:
                obj = Division(id=store.next_id("division"),
                               external_ref=code, **values)
                store.add_division(obj)
                divisions[code] = obj
                counts["divisions"]["created"] += 1
                audit("division_created", "division", obj,
                      {"season_id": season.id,
                       "level_id": level.id if level else None})
            else:
                changed = _apply_changes(obj, values)
                if changed:
                    store.save_division(obj)
                    counts["divisions"]["updated"] += 1
                    audit("division_updated", "division", obj,
                          {"season_id": season.id,
                           "level_id": level.id if level else None,
                           "changed_fields": changed})
                else:
                    counts["divisions"]["skipped"] += 1

        # Permanent league teams (#180): keyed by team_code, owned by a league,
        # carrying no division. Only league + name converge on re-import. Club
        # association is deferred from this slice (#214 review) — Club has no
        # stable external code, so the import never sets or clears club_id — and
        # the legacy division_id/division compatibility fields are preserved
        # (review-4), cleared only by the later migration that removes their
        # readers. Both are left untouched on update.
        for code, row in _group_first(
                rows["permanent_teams"], "team_code").items():
            league = leagues[_clean(row.get("league_code"))]
            values = {"name": _clean(row.get("team_name")),
                      "league_id": league.id}
            obj = teams.get(code)
            if obj is None:
                # A genuinely NEW permanent team carries no club and no division
                # (#180) — participation lives in a registration.
                obj = Team(id=store.next_id("team"), external_ref=code,
                           club_id=None, division_id=None, division="", **values)
                store.add_team(obj)
                teams[code] = obj
                counts["permanent_teams"]["created"] += 1
                audit("team_created", "team", obj, {"league_id": league.id})
            else:
                old_league_id = obj.league_id
                changed = _apply_changes(obj, values)
                if changed:
                    store.save_team(obj)
                    counts["permanent_teams"]["updated"] += 1
                    detail = {"league_id": league.id, "changed_fields": changed}
                    if "league_id" in changed:  # exact from/to on a league move
                        detail["from_league_id"] = old_league_id
                        detail["to_league_id"] = league.id
                    audit("team_updated", "team", obj, detail)
                else:
                    counts["permanent_teams"]["skipped"] += 1

        # Season registrations (#180): one per (season, team), assigning the
        # season's division. SeasonTeamRegistration has no external code — the
        # (season_id, team_id) pair is its identity, so a repeat import updates
        # the division in place rather than duplicating.
        for row in rows["registrations"]:
            season = seasons[_clean(row.get("season_code"))]
            team = teams[_clean(row.get("team_code"))]
            division = divisions[_clean(row.get("division_code"))]
            reg = store.registration_for_team_in_season(season.id, team.id)
            if reg is None:
                reg = SeasonTeamRegistration(
                    id=store.next_id("streg"), season_id=season.id,
                    team_id=team.id, division_id=division.id, active=True)
                store.add_season_team_registration(reg)
                counts["registrations"]["created"] += 1
                setup._audit(
                    "season_team_registered", "season_team_registration", reg.id,
                    actor_id, {"season_id": season.id, "team_id": team.id,
                               "division_id": division.id,
                               "import_batch_id": batch_id})
            else:
                changed = []
                old_division_id = reg.division_id
                if reg.division_id != division.id:
                    reg.division_id = division.id
                    changed.append("division_id")
                if not reg.active:
                    reg.active = True
                    changed.append("active")
                if changed:
                    store.save_season_team_registration(reg)
                    counts["registrations"]["updated"] += 1
                    detail = {"season_id": season.id, "team_id": team.id,
                              "division_id": division.id,
                              "changed_fields": changed,
                              "import_batch_id": batch_id}
                    if "division_id" in changed:  # exact from/to on a move
                        detail["from_division_id"] = old_division_id
                        detail["to_division_id"] = division.id
                    setup._audit(
                        "season_team_registration_updated",
                        "season_team_registration", reg.id, actor_id, detail)
                else:
                    counts["registrations"]["skipped"] += 1

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
