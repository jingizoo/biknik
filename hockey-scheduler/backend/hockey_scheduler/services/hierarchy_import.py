"""Idempotent client hierarchy CSV import (#174 PR E2).

Four CSV sheets describe the durable setup hierarchy before teams and players:
organizations, leagues, venues+rinks, and competition structure. Validation is
read-only and resolves references against both the same upload and existing
external_ref values. Commit re-runs validation and applies the whole batch in a
single store transaction; missing rows are never treated as deletes.
"""

from collections import OrderedDict
from typing import Dict, Iterable, List, Optional, Tuple

from ..domain import Division, League, Level, Organization, Rink, Season, Venue


HIERARCHY_SHEET_NAMES = (
    "organizations", "leagues", "venues_rinks", "competition")
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

    existing_orgs = _existing_map(
        report, store.all_organizations(), "organization")
    existing_leagues = _existing_map(report, store.all_leagues(), "league")
    _existing_map(report, store.all_venues(), "venue")
    _existing_map(report, store.all_rinks(), "rink")
    _existing_map(report, store.all_seasons(), "season")
    _existing_map(report, store.all_levels(), "level")
    _existing_map(report, store.all_divisions(), "division")

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

    entity_summary = {
        "organizations": len(rows["organizations"]),
        "leagues": len(rows["leagues"]),
        "venues": len(_group_first(rows["venues_rinks"], "venue_code")),
        "rinks": len(rows["venues_rinks"]),
        "seasons": len(_group_first(rows["competition"], "season_code")),
        "levels": len(_group_first(level_rows, "level_code")),
        "divisions": len(rows["competition"]),
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
                "levels", "divisions")}


def commit_hierarchy_import(setup, sheets: Dict[str, List[dict]],
                            actor_id: Optional[str] = None) -> dict:
    """Revalidate and atomically upsert the complete hierarchy batch."""
    result = validate_hierarchy_import(sheets, setup.store)
    if not result["ok"]:
        return {
            "committed": False,
            "import_type": "hierarchy",
            "summary": _new_counts(),
            "errors": result["errors"],
            "warnings": result["warnings"],
        }

    store = setup.store
    rows = {name: list((sheets or {}).get(name) or [])
            for name in HIERARCHY_SHEET_NAMES}
    counts = _new_counts()

    with store.transaction():
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
