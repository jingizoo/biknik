"""Canonical nine-sheet CSV fixture builders (#260 Slice F).

Centralizes ADR 0001 / issue #260 sheet vocabulary in one place so tests
build payloads by intent — which sheets, which rows — instead of
duplicating full CSV strings across dozens of test methods. Column order
mirrors ``hierarchy_import.HIERARCHY_TEMPLATES`` exactly.
"""

ORGANIZATIONS_HEADER = "organization_code,organization_name,short_name"
PROGRAMS_HEADER = (
    "program_code,operator_organization_code,program_name,country,timezone")
VENUES_RINKS_HEADER = (
    "venue_code,organization_code,venue_name,address,timezone,"
    "rink_code,rink_name")
COMPETITION_HEADER = (
    "program_code,season_code,season_name,league_code,league_name,"
    "league_sort_order,division_code,division_name,age_group")
CLUBS_HEADER = "club_code,club_name,country"
PERMANENT_TEAMS_HEADER = "program_code,league_code,team_code,team_name,club_code"
PLAYERS_HEADER = (
    "player_code,team_code,first_name,last_name,jersey_number,position,email")
REGISTRATIONS_HEADER = "season_code,team_code,league_code,division_code"
SEASON_VENUE_ACCESS_HEADER = "season_code,venue_code,active"


def _csv(header, rows):
    return "\n".join((header,) + tuple(rows)) + "\n"


def organizations_csv(rows=("CANLON,Canlon Ice Facilities,Canlon",)):
    return _csv(ORGANIZATIONS_HEADER, rows)


def programs_csv(rows=("OVER55,CANLON,Over 55,US,America/Chicago",)):
    return _csv(PROGRAMS_HEADER, rows)


def venues_rinks_csv(rows=(
        "PLAINFIELD,CANLON,Plainfield Ice,123 Main St,America/Chicago,PF1,Rink 1",
        "PLAINFIELD,CANLON,Plainfield Ice,123 Main St,America/Chicago,PF2,Rink 2")):
    return _csv(VENUES_RINKS_HEADER, rows)


def competition_csv(rows=(
        "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVA,Division A,Adult",
        "OVER55,FALL26,Fall 2026,L1,Adult League,1,DIVB,Division B,Adult")):
    return _csv(COMPETITION_HEADER, rows)


def clubs_csv(rows=("EAGLES,Eagles HC,US",)):
    return _csv(CLUBS_HEADER, rows)


def permanent_teams_csv(rows=(
        "OVER55,L1,LIONS,Lions,EAGLES",
        "OVER55,L1,BEARS,Bears,")):
    return _csv(PERMANENT_TEAMS_HEADER, rows)


def players_csv(rows=("P001,LIONS,Jane,Smith,7,forward,jane@example.com",)):
    return _csv(PLAYERS_HEADER, rows)


def registrations_csv(rows=(
        "FALL26,LIONS,L1,DIVA",
        "FALL26,BEARS,L1,DIVA")):
    return _csv(REGISTRATIONS_HEADER, rows)


def season_venue_access_csv(rows=("FALL26,PLAINFIELD,true",)):
    return _csv(SEASON_VENUE_ACCESS_HEADER, rows)


def full_payload(**overrides):
    """The complete, cross-referencing canonical nine-sheet payload.

    Pass e.g. ``clubs_csv=""`` to omit a sheet, or any of the ``*_csv``
    builder functions above (with custom ``rows``) to replace one.
    """
    body = {
        "import_type": "hierarchy",
        "organizations_csv": organizations_csv(),
        "programs_csv": programs_csv(),
        "venues_rinks_csv": venues_rinks_csv(),
        "competition_csv": competition_csv(),
        "clubs_csv": clubs_csv(),
        "permanent_teams_csv": permanent_teams_csv(),
        "players_csv": players_csv(),
        "registrations_csv": registrations_csv(),
        "season_venue_access_csv": season_venue_access_csv(),
    }
    body.update(overrides)
    return body


def by_ref(rows, code):
    return next(row for row in rows if row.external_ref == code)
