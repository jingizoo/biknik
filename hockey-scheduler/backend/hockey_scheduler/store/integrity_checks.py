"""Pre-migration data validation for #201 DB-enforced invariants.

Before a forward-only uniqueness/constraint migration is applied, the
corresponding check here runs against the live data and *reports* any rows that
would violate the new constraint — it never guesses a fix or deletes data. An
upgrade against dirty data then fails loudly, naming the offending records, so
an operator can resolve them, rather than surfacing an opaque index-creation
error from the driver.

These checks are keyed to a migration version and invoked by ``migrate()`` just
before that version's statements run (see ``_PRE_MIGRATION_CHECKS`` in
``sql_store``). They are plain SELECTs — no writes — so they are safe to re-run.
"""

from ..domain.jersey import MAX_JERSEY_NUMBER, MIN_JERSEY_NUMBER


class MigrationDataError(RuntimeError):
    """Existing data would violate a constraint a migration is about to add."""


def find_duplicate_active_ice_slots(conn):
    """Ice slot ids that already back more than one active (non-cancelled) game."""
    cur = conn.cursor()
    cur.execute(
        "SELECT ice_slot_id FROM games "
        "WHERE cancelled = 0 AND ice_slot_id IS NOT NULL "
        "GROUP BY ice_slot_id HAVING COUNT(*) > 1")
    return sorted(row["ice_slot_id"] for row in cur.fetchall())


def assert_no_duplicate_active_ice_slots(conn):
    """Abort the migration if any ice slot has multiple active games (#201 3A)."""
    duplicates = find_duplicate_active_ice_slots(conn)
    if duplicates:
        shown = ", ".join(duplicates[:20])
        more = "" if len(duplicates) <= 20 else f" (+{len(duplicates) - 20} more)"
        raise MigrationDataError(
            "Cannot enforce one active game per ice slot: "
            f"{len(duplicates)} ice slot(s) already have multiple active games: "
            f"{shown}{more}. Cancel or move the extra games before upgrading.")


def find_duplicate_roster_players(conn):
    """Concrete (game_id, player_id) pairs with more than one roster row.

    Only non-null pairs are considered — matching the partial unique index
    (migration 023), which excludes NULL-bearing rows because both SQLite and
    PostgreSQL treat NULLs as distinct. Filtering here keeps the check aligned
    with what the index enforces and avoids ordering mixed None/str tuples.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT game_id, player_id FROM game_roster_entries "
        "WHERE game_id IS NOT NULL AND player_id IS NOT NULL "
        "GROUP BY game_id, player_id HAVING COUNT(*) > 1")
    return sorted((row["game_id"], row["player_id"]) for row in cur.fetchall())


def assert_no_duplicate_roster_players(conn):
    """Abort the migration if any (game, player) has multiple roster rows (#201 3B)."""
    duplicates = find_duplicate_roster_players(conn)
    if duplicates:
        pairs = [f"{game}/{player}" for game, player in duplicates[:20]]
        more = "" if len(duplicates) <= 20 else f" (+{len(duplicates) - 20} more)"
        raise MigrationDataError(
            "Cannot enforce one roster row per player per game: "
            f"{len(duplicates)} (game, player) pair(s) already have duplicate "
            f"roster rows: {', '.join(pairs)}{more}. Resolve the duplicates "
            "before upgrading.")


def find_duplicate_result_games(conn):
    """Concrete game_ids that already back more than one result row.

    Only non-null game_ids are considered — matching the partial unique index
    (migration 024), which excludes NULL-bearing rows because both SQLite and
    PostgreSQL treat NULLs as distinct. Filtering here keeps the check aligned
    with what the index enforces.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT game_id FROM game_results "
        "WHERE game_id IS NOT NULL "
        "GROUP BY game_id HAVING COUNT(*) > 1")
    return sorted(row["game_id"] for row in cur.fetchall())


def assert_no_duplicate_result_games(conn):
    """Abort the migration if any game has multiple result rows (#201 3C)."""
    duplicates = find_duplicate_result_games(conn)
    if duplicates:
        shown = ", ".join(duplicates[:20])
        more = "" if len(duplicates) <= 20 else f" (+{len(duplicates) - 20} more)"
        raise MigrationDataError(
            "Cannot enforce one result per game: "
            f"{len(duplicates)} game(s) already have multiple result rows: "
            f"{shown}{more}. Resolve the duplicates before upgrading.")


def find_orphan_result_games(conn):
    """Non-null game_id values on result rows that reference a missing game.

    These are the rows a game_results.game_id → games(id) foreign key (migration
    025) would reject. NULL game_ids are excluded: a nullable foreign key permits
    NULL (a result not yet tied to a game), so only dangling concrete references
    block the upgrade.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT gr.game_id FROM game_results gr "
        "LEFT JOIN games g ON g.id = gr.game_id "
        "WHERE gr.game_id IS NOT NULL AND g.id IS NULL")
    return sorted(row["game_id"] for row in cur.fetchall())


def assert_result_games_exist(conn):
    """Abort the migration if any result references a missing game (#201 3D)."""
    orphans = find_orphan_result_games(conn)
    if orphans:
        shown = ", ".join(orphans[:20])
        more = "" if len(orphans) <= 20 else f" (+{len(orphans) - 20} more)"
        raise MigrationDataError(
            "Cannot add the result → game foreign key: "
            f"{len(orphans)} result row(s) reference a game that does not "
            f"exist: {shown}{more}. Reattach or remove them before upgrading.")


def find_results_missing_game(conn):
    """Result row ids whose game_id is NULL (would fail the NOT NULL migration)."""
    cur = conn.cursor()
    cur.execute("SELECT id FROM game_results WHERE game_id IS NULL")
    return sorted(row["id"] for row in cur.fetchall())


def assert_results_have_game(conn):
    """Abort the migration if any result has no game_id (#201 3E)."""
    missing = find_results_missing_game(conn)
    if missing:
        shown = ", ".join(missing[:20])
        more = "" if len(missing) <= 20 else f" (+{len(missing) - 20} more)"
        raise MigrationDataError(
            "Cannot require a game for every result: "
            f"{len(missing)} result row(s) have no game_id: {shown}{more}. "
            "Attach them to a game or remove them before upgrading.")


def find_orphan_roster_refs(conn):
    """Roster row ids whose non-null game_id or player_id names a missing parent.

    These are the rows the game_roster_entries → games/players foreign keys
    (migration 027) would reject. NULLs are excluded: a nullable foreign key
    permits NULL, so only dangling concrete references block the upgrade.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT r.id FROM game_roster_entries r "
        "LEFT JOIN games g ON g.id = r.game_id "
        "LEFT JOIN players p ON p.id = r.player_id "
        "WHERE (r.game_id IS NOT NULL AND g.id IS NULL) "
        "   OR (r.player_id IS NOT NULL AND p.id IS NULL)")
    return sorted(row["id"] for row in cur.fetchall())


def assert_roster_refs_exist(conn):
    """Abort the migration if any roster row references a missing game/player
    (#201 3F)."""
    orphans = find_orphan_roster_refs(conn)
    if orphans:
        shown = ", ".join(orphans[:20])
        more = "" if len(orphans) <= 20 else f" (+{len(orphans) - 20} more)"
        raise MigrationDataError(
            "Cannot add the roster → game/player foreign keys: "
            f"{len(orphans)} roster row(s) reference a game or player that does "
            f"not exist: {shown}{more}. Reattach or remove them before upgrading.")


# -- #201 Slice 2 — reassignment referential integrity preflight -----------
#
# Migration 040 adds players.team_id → teams(id) and teams.club_id → clubs(id).
# These read-only checks report every non-null player.team_id that names no team
# and every non-null teams.club_id that names no club BEFORE the constraints are
# added, so an upgrade against dangling data fails loudly with the offending row
# ids rather than an opaque constraint error. NULLs are excluded: a nullable
# foreign key permits NULL, so only a concrete dangling reference blocks the
# upgrade. Pure SELECTs, portable across SQLite/PostgreSQL, safe to re-run.


def find_orphan_player_teams(conn):
    """Player ids whose non-null team_id names no existing team (migration 040)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT p.id FROM players p "
        "LEFT JOIN teams t ON t.id = p.team_id "
        "WHERE p.team_id IS NOT NULL AND t.id IS NULL")
    return sorted(row["id"] for row in cur.fetchall())


def find_orphan_team_clubs(conn):
    """Team ids whose non-null club_id names no existing club (migration 040)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT t.id FROM teams t "
        "LEFT JOIN clubs c ON c.id = t.club_id "
        "WHERE t.club_id IS NOT NULL AND c.id IS NULL")
    return sorted(row["id"] for row in cur.fetchall())


def assert_reassignment_fks_ready(conn):
    """Abort migration 040 if any player references a missing team or any team
    references a missing club (#201 Slice 2)."""
    orphan_players = find_orphan_player_teams(conn)
    orphan_teams = find_orphan_team_clubs(conn)
    if not orphan_players and not orphan_teams:
        return

    problems = []
    if orphan_players:
        shown = ", ".join(orphan_players[:20])
        more = ("" if len(orphan_players) <= 20
                else f" (+{len(orphan_players) - 20} more)")
        problems.append(
            f"{len(orphan_players)} player(s) reference a team that does not "
            f"exist: {shown}{more}")
    if orphan_teams:
        shown = ", ".join(orphan_teams[:20])
        more = ("" if len(orphan_teams) <= 20
                else f" (+{len(orphan_teams) - 20} more)")
        problems.append(
            f"{len(orphan_teams)} team(s) reference a club that does not "
            f"exist: {shown}{more}")
    raise MigrationDataError(
        "Cannot add the player → team / team → club foreign keys: "
        + "; ".join(problems)
        + ". Reattach or clear these references before upgrading.")


# -- #201 Slice 3 — IceSlot / venue facility-hierarchy integrity preflight ---
#
# Migration 041 adds rinks.venue_id → venues(id), ice_slots.rink_id → rinks(id),
# games.ice_slot_id → ice_slots(id), and season_venue_access.venue_id →
# venues(id). These read-only checks report every non-null child reference that
# names no parent BEFORE the constraints are added, so an upgrade against
# dangling data fails loudly with the offending row ids rather than an opaque
# constraint error. NULLs are excluded: a nullable foreign key permits NULL, so
# only a concrete dangling reference blocks the upgrade. Pure SELECTs, portable
# across SQLite/PostgreSQL, safe to re-run.


def find_orphan_rink_venues(conn):
    """Rink ids whose non-null venue_id names no existing venue (migration 041)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT r.id FROM rinks r "
        "LEFT JOIN venues v ON v.id = r.venue_id "
        "WHERE r.venue_id IS NOT NULL AND v.id IS NULL")
    return sorted(row["id"] for row in cur.fetchall())


def find_orphan_ice_slot_rinks(conn):
    """Ice-slot ids whose non-null rink_id names no existing rink (migration 041)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT s.id FROM ice_slots s "
        "LEFT JOIN rinks r ON r.id = s.rink_id "
        "WHERE s.rink_id IS NOT NULL AND r.id IS NULL")
    return sorted(row["id"] for row in cur.fetchall())


def find_orphan_game_ice_slots(conn):
    """Game ids whose non-null ice_slot_id names no existing ice slot (migration 041)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT g.id FROM games g "
        "LEFT JOIN ice_slots s ON s.id = g.ice_slot_id "
        "WHERE g.ice_slot_id IS NOT NULL AND s.id IS NULL")
    return sorted(row["id"] for row in cur.fetchall())


def find_orphan_sva_venues(conn):
    """SeasonVenueAccess ids whose venue_id names no existing venue (migration 041)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT a.id FROM season_venue_access a "
        "LEFT JOIN venues v ON v.id = a.venue_id "
        "WHERE a.venue_id IS NOT NULL AND v.id IS NULL")
    return sorted(row["id"] for row in cur.fetchall())


def assert_iceslot_venue_fks_ready(conn):
    """Abort migration 041 if any rink references a missing venue, any ice slot a
    missing rink, any game a missing ice slot, or any season-venue-access a
    missing venue (#201 Slice 3)."""
    orphan_rinks = find_orphan_rink_venues(conn)
    orphan_slots = find_orphan_ice_slot_rinks(conn)
    orphan_games = find_orphan_game_ice_slots(conn)
    orphan_access = find_orphan_sva_venues(conn)
    if not (orphan_rinks or orphan_slots or orphan_games or orphan_access):
        return

    def _describe(ids, noun, parent):
        shown = ", ".join(ids[:20])
        more = "" if len(ids) <= 20 else f" (+{len(ids) - 20} more)"
        return (f"{len(ids)} {noun} reference a {parent} that does not exist: "
                f"{shown}{more}")

    problems = []
    if orphan_rinks:
        problems.append(_describe(orphan_rinks, "rink(s)", "venue"))
    if orphan_slots:
        problems.append(_describe(orphan_slots, "ice slot(s)", "rink"))
    if orphan_games:
        problems.append(_describe(orphan_games, "game(s)", "ice slot"))
    if orphan_access:
        problems.append(_describe(orphan_access, "season-venue-access row(s)",
                                  "venue"))
    raise MigrationDataError(
        "Cannot add the rink → venue / ice slot → rink / game → ice slot / "
        "season-venue-access → venue foreign keys: " + "; ".join(problems)
        + ". Reattach or clear these references before upgrading.")


# -- #233 Slice C — competition-model reset reparent preflight -------------
#
# Slice C reparents each Division onto a League (today's `levels`) and derives a
# `league_id` for every SeasonTeamRegistration. ADR 0001 requires the upgrade to
# *abort and report* any row it cannot map from a validated, same-Season chain,
# rather than guess (no silent reassignment, no cross-season League). Because
# these setup relationships still have no DB foreign keys, legacy/directly-loaded
# rows can carry a dangling or cross-season `level_id`/`division_id`; a non-null
# id is therefore NOT proof of a valid derivation. These read-only checks
# validate the whole Division→League and Registration→League chain and return
# structured diagnostics; the reparent migration (Slice C1b) registers
# ``assert_competition_reset_ready`` as its pre-migration gate.
#
# Scope: these checks validate the competition reparent chain
# (Division→Season/Level, Registration→Season/Division/Level) only. Team→Program
# and other one-to-one reference integrity is out of scope here — the Team
# reparent lands in C1b with its own validation. They are pure SELECTs, portable
# across SQLite/PostgreSQL, and safe to re-run.


def find_undeterminable_division_leagues(conn):
    """Divisions that can't be deterministically reparented onto a League.

    Reports a Division whose parent chain is invalid or ambiguous, with a
    ``reason``:
      - ``missing_season``     — its ``season_id`` references no Season;
      - ``dangling_level``     — its non-null ``level_id`` references no Level;
      - ``cross_season_level`` — its Level belongs to a different Season;
      - ``no_single_league``   — it has no ``level_id`` and its Season has 0 or
        >1 Leagues, so there is no sole League to attach it to.
    A Division whose non-null ``level_id`` resolves to a same-Season Level is
    deterministic and is not reported. Returns a list of dicts:
    ``{division_id, season_id, level_id, level_count, reason}``.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT d.id AS division_id, d.season_id AS season_id, "
        "d.level_id AS level_id, "
        "(SELECT COUNT(*) FROM seasons s WHERE s.id = d.season_id) AS season_exists, "
        "(SELECT COUNT(*) FROM levels lv WHERE lv.season_id = d.season_id) "
        "AS level_count, "
        "(SELECT lv.season_id FROM levels lv WHERE lv.id = d.level_id) AS level_season "
        "FROM divisions d")
    issues = []
    for row in cur.fetchall():
        level_id = row["level_id"]
        if not row["season_exists"]:
            reason = "missing_season"
        elif level_id is not None and row["level_season"] is None:
            reason = "dangling_level"
        elif level_id is not None and row["level_season"] != row["season_id"]:
            reason = "cross_season_level"
        elif level_id is None and row["level_count"] != 1:
            reason = "no_single_league"
        else:
            continue
        issues.append({"division_id": row["division_id"], "season_id": row["season_id"],
                       "level_id": level_id, "level_count": row["level_count"],
                       "reason": reason})
    return sorted(issues, key=lambda x: x["division_id"])


def find_underivable_registration_leagues(conn):
    """Registrations whose League can't be derived from a validated same-Season chain.

    Reports a registration whose derivation chain is invalid or ambiguous, with a
    ``reason``:
      - ``missing_season``        — its ``season_id`` references no Season;
      - ``dangling_division``     — its non-null ``division_id`` references no Division;
      - ``cross_season_division`` — its Division belongs to a different Season;
      - ``dangling_level``        — its Division's ``level_id`` references no Level;
      - ``cross_season_level``    — its Division's Level belongs to a different Season;
      - ``no_single_league``      — it has no usable Division-level and its Season
        has 0 or >1 Leagues.
    A registration that resolves via a same-Season Division→Level, or (absent a
    usable Division-level) via a Season with exactly one League, is deterministic
    and is not reported. Returns a list of dicts:
    ``{registration_id, season_id, division_id, level_id, level_count, reason}``.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT r.id AS registration_id, r.season_id AS season_id, "
        "r.division_id AS division_id, "
        "(SELECT COUNT(*) FROM seasons s WHERE s.id = r.season_id) AS season_exists, "
        "(SELECT COUNT(*) FROM levels lv WHERE lv.season_id = r.season_id) "
        "AS level_count, "
        "(SELECT d.season_id FROM divisions d WHERE d.id = r.division_id) AS div_season, "
        "(SELECT d.level_id FROM divisions d WHERE d.id = r.division_id) AS div_level_id, "
        "(SELECT lv.season_id FROM levels lv WHERE lv.id = "
        "  (SELECT d.level_id FROM divisions d WHERE d.id = r.division_id)) "
        "AS div_level_season "
        "FROM season_team_registrations r")
    issues = []
    for row in cur.fetchall():
        did = row["division_id"]
        div_level_id = row["div_level_id"]
        # Report the Division's actual Level id in every diagnostic (None only
        # when there is no Division or the Division carries no Level), regardless
        # of which reason branch fires.
        level_id = div_level_id
        if not row["season_exists"]:
            reason = "missing_season"
        elif did is not None and row["div_season"] is None:
            reason = "dangling_division"
        elif did is not None and row["div_season"] != row["season_id"]:
            reason = "cross_season_division"
        elif did is not None and div_level_id is not None:
            # The (same-Season) Division carries a Level — that Level must itself
            # exist and belong to this Season for a deterministic derivation.
            if row["div_level_season"] is None:
                reason = "dangling_level"
            elif row["div_level_season"] != row["season_id"]:
                reason = "cross_season_level"
            else:
                continue  # derivable via a validated same-Season Division→Level
        elif row["level_count"] == 1:
            continue  # derivable via the Season's sole League
        else:
            reason = "no_single_league"
        issues.append({"registration_id": row["registration_id"],
                       "season_id": row["season_id"], "division_id": did,
                       "level_id": level_id, "level_count": row["level_count"],
                       "reason": reason})
    return sorted(issues, key=lambda x: x["registration_id"])


def assert_competition_reset_ready(conn):
    """Abort the competition-model reset (#233 Slice C) if any Division or
    registration can't be reparented onto a League from a validated same-Season
    chain.

    Read-only: raises :class:`MigrationDataError` with bounded, row-level
    diagnostics (id + Season + Level/candidate count + reason) and leaves all
    data unchanged, so an operator resolves each ambiguous/invalid row before the
    reparent migration runs.
    """
    div_issues = find_undeterminable_division_leagues(conn)
    reg_issues = find_underivable_registration_leagues(conn)
    if not div_issues and not reg_issues:
        return
    lines = []
    for d in div_issues[:20]:
        lines.append(
            f"division {d['division_id']} (season={d['season_id']}, "
            f"level={d['level_id']}, leagues_in_season={d['level_count']}, "
            f"reason={d['reason']})")
    if len(div_issues) > 20:
        lines.append(f"(+{len(div_issues) - 20} more division(s))")
    for r in reg_issues[:20]:
        lines.append(
            f"registration {r['registration_id']} (season={r['season_id']}, "
            f"division={r['division_id']}, level={r['level_id']}, "
            f"leagues_in_season={r['level_count']}, reason={r['reason']})")
    if len(reg_issues) > 20:
        lines.append(f"(+{len(reg_issues) - 20} more registration(s))")
    raise MigrationDataError(
        f"Cannot reset the competition model (#233 Slice C): {len(div_issues)} "
        f"division(s) and {len(reg_issues)} registration(s) cannot be "
        "deterministically reparented onto a same-Season League. Resolve these "
        "before upgrading — " + "; ".join(lines))


# -- #233 Slice C1b — the rest of migration 028's rename/reparent/backfill -----
#
# C1a (above) validates only the Division and Registration reparent chain. C1b
# also renames seasons.league_id -> program_id and teams.league_id -> program_id
# (both pointing at the umbrella `leagues` row that becomes a `programs` row),
# promotes `levels` -> `leagues`, and backfills every Game's competition
# league_id. These checks validate the rest so migration 028 never leaves an
# unscoped canonical row.
#
# They run against the PRE-028 schema: `leagues` is still the umbrella,
# `seasons.league_id`/`teams.league_id` reference it, `levels` is the grouping
# (promoted to `leagues` by 028), `divisions.level_id` references `levels`, and
# neither games nor registrations carry a league_id yet. A non-null id is not
# proof of a valid parent — these setup relationships have no DB foreign keys, so
# a legacy row can carry a dangling reference. Pure SELECTs, portable across
# SQLite/PostgreSQL, safe to re-run.


def find_seasons_missing_program(conn):
    """Seasons whose umbrella parent is gone (pre-028 ``seasons.league_id``).

    A season carries a non-null ``league_id`` that references no ``leagues``
    (umbrella) row — after the rename it would be a Program-less Season. Returns
    ``[{season_id, program_id, reason: 'missing_program'}]`` (``program_id`` is
    the offending pre-028 ``league_id`` value).
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT s.id AS season_id, s.league_id AS program_id "
        "FROM seasons s "
        "WHERE s.league_id IS NOT NULL "
        "  AND NOT EXISTS (SELECT 1 FROM leagues lg WHERE lg.id = s.league_id)")
    return sorted(
        ({"season_id": r["season_id"], "program_id": r["program_id"],
          "reason": "missing_program"} for r in cur.fetchall()),
        key=lambda x: x["season_id"])


def find_teams_missing_program(conn):
    """Teams whose umbrella owner is gone (pre-028 ``teams.league_id``).

    A team carries a non-null ``league_id`` that references no ``leagues``
    (umbrella) row — after the rename it would be a Program-less Team. Returns
    ``[{team_id, program_id, reason: 'missing_program'}]``.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT t.id AS team_id, t.league_id AS program_id "
        "FROM teams t "
        "WHERE t.league_id IS NOT NULL "
        "  AND NOT EXISTS (SELECT 1 FROM leagues lg WHERE lg.id = t.league_id)")
    return sorted(
        ({"team_id": r["team_id"], "program_id": r["program_id"],
          "reason": "missing_program"} for r in cur.fetchall()),
        key=lambda x: x["team_id"])


def find_promoted_leagues_missing_season(conn):
    """Promoted Leagues (pre-028 ``levels``) whose Season parent is gone.

    A ``levels`` row carries a non-null ``season_id`` that references no
    ``seasons`` row — after promotion it would be a Season-less League. Returns
    ``[{league_id, season_id, reason: 'missing_season'}]`` (``league_id`` is the
    pre-028 ``levels.id``).
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT lv.id AS league_id, lv.season_id AS season_id "
        "FROM levels lv "
        "WHERE lv.season_id IS NOT NULL "
        "  AND NOT EXISTS (SELECT 1 FROM seasons s WHERE s.id = lv.season_id)")
    return sorted(
        ({"league_id": r["league_id"], "season_id": r["season_id"],
          "reason": "missing_season"} for r in cur.fetchall()),
        key=lambda x: x["league_id"])


def find_underivable_game_leagues(conn):
    """Games whose competition League can't be derived for migration 028's backfill.

    Migration 028 derives a game's ``league_id`` from its Division→League when the
    game has a division, else from the game's Season sole League. This reports a
    game where that derivation is invalid or ambiguous, with a ``reason``:
      - ``dangling_division``     — its non-null ``division_id`` references no Division;
      - ``cross_season_division`` — it has a Season and its Division belongs to a
        different Season;
      - ``missing_season``        — it is division-less and its ``season_id`` is
        null or references no Season (no League to derive from);
      - ``no_single_league``      — it is division-less and its Season has 0 or >1
        Leagues (``levels``), so there is no sole League to attach it to.
    A division-backed game whose Division resolves in the game's Season (or which
    has no Season to cross-check) derives via that Division — the Division's own
    reparent is validated by the C1a division check. Returns a list of dicts:
    ``{game_id, season_id, division_id, level_count, reason}``.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT g.id AS game_id, g.season_id AS season_id, "
        "g.division_id AS division_id, "
        "(SELECT COUNT(*) FROM seasons s WHERE s.id = g.season_id) AS season_exists, "
        "(SELECT COUNT(*) FROM levels lv WHERE lv.season_id = g.season_id) "
        "AS level_count, "
        "(SELECT d.season_id FROM divisions d WHERE d.id = g.division_id) "
        "AS div_season "
        "FROM games g")
    issues = []
    for row in cur.fetchall():
        did = row["division_id"]
        sid = row["season_id"]
        if did is not None:
            if row["div_season"] is None:
                reason = "dangling_division"
            elif sid is not None and row["div_season"] != sid:
                reason = "cross_season_division"
            else:
                continue  # derives via a same-Season (or Season-less) Division
        elif sid is None or not row["season_exists"]:
            reason = "missing_season"
        elif row["level_count"] != 1:
            reason = "no_single_league"
        else:
            continue  # derives via the Season's sole League
        issues.append({"game_id": row["game_id"], "season_id": sid,
                       "division_id": did, "level_count": row["level_count"],
                       "reason": reason})
    return sorted(issues, key=lambda x: x["game_id"])


def assert_competition_reset_ready_c1b(conn):
    """Abort migration 028 (#233 Slice C1b) if ANY row it renames/reparents/
    backfills cannot be mapped deterministically onto the canonical model.

    A superset of :func:`assert_competition_reset_ready` (C1a's Division +
    Registration reparent): it additionally validates the Season/Team umbrella
    parents, the promoted-League Season parent, and every Game's derived League.
    Read-only — raises :class:`MigrationDataError` with bounded, row-level
    diagnostics (id + parent id + reason) and leaves all data unchanged.
    """
    # C1a first (Division + Registration reparent), so its detailed diagnostics
    # surface exactly as before when they are the problem.
    assert_competition_reset_ready(conn)

    season_issues = find_seasons_missing_program(conn)
    team_issues = find_teams_missing_program(conn)
    league_issues = find_promoted_leagues_missing_season(conn)
    game_issues = find_underivable_game_leagues(conn)
    if not (season_issues or team_issues or league_issues or game_issues):
        return

    lines = []
    for s in season_issues[:20]:
        lines.append(f"season {s['season_id']} (program={s['program_id']}, "
                     f"reason={s['reason']})")
    if len(season_issues) > 20:
        lines.append(f"(+{len(season_issues) - 20} more season(s))")
    for t in team_issues[:20]:
        lines.append(f"team {t['team_id']} (program={t['program_id']}, "
                     f"reason={t['reason']})")
    if len(team_issues) > 20:
        lines.append(f"(+{len(team_issues) - 20} more team(s))")
    for lg in league_issues[:20]:
        lines.append(f"league {lg['league_id']} (season={lg['season_id']}, "
                     f"reason={lg['reason']})")
    if len(league_issues) > 20:
        lines.append(f"(+{len(league_issues) - 20} more league(s))")
    for g in game_issues[:20]:
        lines.append(f"game {g['game_id']} (season={g['season_id']}, "
                     f"division={g['division_id']}, "
                     f"leagues_in_season={g['level_count']}, "
                     f"reason={g['reason']})")
    if len(game_issues) > 20:
        lines.append(f"(+{len(game_issues) - 20} more game(s))")
    raise MigrationDataError(
        f"Cannot reset the competition model (#233 Slice C1b): "
        f"{len(season_issues)} season(s), {len(team_issues)} team(s), "
        f"{len(league_issues)} league(s), and {len(game_issues)} game(s) "
        "reference a missing parent or cannot be deterministically scoped onto a "
        "same-Season League. Resolve these before upgrading — " + "; ".join(lines))


# -- #233 Slice E — Venue/Program legacy-link backfill preflight -----------
#
# Migration 029 backfills a SeasonVenueAccess row from the legacy
# ``venues.league_id`` (Program) link only when that Program has EXACTLY ONE
# Season — the sole Season a venue-wide link could unambiguously have meant.
# A Program with two or more Seasons is ambiguous and must never be guessed
# (ADR 0001 / epic #233: no silent reassignment). This read-only check finds
# every such Venue and aborts the whole migration with per-row diagnostics,
# leaving both ``venues.league_id`` and the new table untouched.


def find_venues_with_ambiguous_program_seasons(conn):
    """Venues whose legacy Program link resolves to more than one Season.

    A non-null ``venues.league_id`` (a Program id) with two or more Season
    rows sharing that ``program_id`` has no single Season the legacy link
    could unambiguously mean, so migration 029's backfill can't pick one
    without guessing. Returns ``[{venue_id, program_id, season_count}, ...]``.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT v.id AS venue_id, v.league_id AS program_id, "
        "COUNT(s.id) AS season_count "
        "FROM venues v JOIN seasons s ON s.program_id = v.league_id "
        "WHERE v.league_id IS NOT NULL "
        "GROUP BY v.id, v.league_id HAVING COUNT(s.id) > 1")
    return sorted(
        ({"venue_id": r["venue_id"], "program_id": r["program_id"],
          "season_count": r["season_count"]} for r in cur.fetchall()),
        key=lambda x: x["venue_id"])


def assert_venue_season_access_backfill_ready(conn):
    """Abort migration 029 (#233 Slice E) if any Venue's legacy Program link
    resolves to more than one Season.

    Read-only: raises :class:`MigrationDataError` with bounded, row-level
    diagnostics (venue id + program id + season count) and leaves all data
    unchanged, so an operator resolves each ambiguous venue (or waits for the
    Slice E2 UI to grant SeasonVenueAccess explicitly) before the backfill
    runs. A Venue whose Program has zero or exactly one Season is unaffected
    and is not reported.
    """
    issues = find_venues_with_ambiguous_program_seasons(conn)
    if not issues:
        return
    shown = ", ".join(
        f"venue {i['venue_id']} (program={i['program_id']}, "
        f"seasons={i['season_count']})" for i in issues[:20])
    more = "" if len(issues) <= 20 else f" (+{len(issues) - 20} more)"
    raise MigrationDataError(
        "Cannot backfill Season-Venue access (#233 Slice E): "
        f"{len(issues)} venue(s) have a legacy Program link that resolves to "
        "more than one Season, so a single backfill target can't be chosen "
        f"deterministically: {shown}{more}. Grant SeasonVenueAccess "
        "explicitly for these venues instead of relying on the legacy link, "
        "or resolve the ambiguity before upgrading.")


# --------------------------------------------------------------------------- #
# 035 competition hierarchy reset (#283 Slice A): Program -> League -> Team    #
# with a Season overlay via LeagueSeason.                                      #
# --------------------------------------------------------------------------- #
def _canonical_league_index(conn):
    """Compute the permanent-League merge from the pre-035 schema.

    Today a ``leagues`` row is per-Season. Two rows are the SAME permanent
    League when they share a Program (via their Season) and the same key —
    ``coalesce(external_ref, lower(name))`` — and the lowest id in that group is
    the canonical permanent League. Returns ``(canonical_of, name_of,
    season_of)`` maps keyed by original league id (``canonical_of[lg] `` is the
    id the row will merge into), for reuse by the finders below.
    """
    cur = conn.cursor()
    cur.execute("SELECT id, program_id FROM seasons")
    program_of_season = {r["id"]: r["program_id"] for r in cur.fetchall()}
    cur.execute("SELECT id, season_id, name, external_ref FROM leagues")
    leagues = cur.fetchall()
    name_of, season_of, program_of, key_of = {}, {}, {}, {}
    for lg in leagues:
        lid = lg["id"]
        name_of[lid] = lg["name"]
        season_of[lid] = lg["season_id"]
        program_of[lid] = program_of_season.get(lg["season_id"])
        key_of[lid] = (lg["external_ref"] or (lg["name"] or "").lower())
    # canonical = lexicographically-lowest id within each (program, key) group,
    # matching migration 035's ``MIN(id)``.
    group_min = {}
    for lid in sorted(name_of):
        g = (program_of[lid], key_of[lid])
        if g not in group_min or lid < group_min[g]:
            group_min[g] = lid
    canonical_of = {lid: group_min[(program_of[lid], key_of[lid])]
                    for lid in name_of}
    return canonical_of, name_of, season_of


def _effective_original_league(conn):
    """Map each registration to its effective per-Season league id, mirroring
    migration 035: the registration's own ``league_id`` when set, else the
    Season's sole league (``None`` when the Season has zero or several)."""
    cur = conn.cursor()
    cur.execute("SELECT season_id, id FROM leagues")
    leagues_by_season = {}
    for r in cur.fetchall():
        leagues_by_season.setdefault(r["season_id"], []).append(r["id"])
    cur.execute("SELECT id, season_id, team_id, league_id FROM "
                "season_team_registrations")
    regs = cur.fetchall()
    effective = {}
    for r in regs:
        lid = r["league_id"]
        if lid is None:
            sole = leagues_by_season.get(r["season_id"], [])
            lid = sole[0] if len(sole) == 1 else None
        effective[r["id"]] = (r["team_id"], r["season_id"], lid)
    return effective


def find_ambiguous_team_leagues(conn):
    """Teams whose historical registrations resolve to MORE THAN ONE permanent
    League and that have no operator decision recorded.

    Returns ``[{team_id, team_name, league_ids, league_names, season_ids}, ...]``
    — the exception report migration 035 halts on until each is resolved in
    ``team_league_migration_decisions``.
    """
    canonical_of, name_of, _season_of = _canonical_league_index(conn)
    effective = _effective_original_league(conn)
    cur = conn.cursor()
    cur.execute("SELECT id, name FROM teams")
    team_name = {r["id"]: r["name"] for r in cur.fetchall()}
    cur.execute("SELECT team_id FROM team_league_migration_decisions")
    decided = {r["team_id"] for r in cur.fetchall()}

    by_team = {}
    for team_id, season_id, orig_lid in effective.values():
        if orig_lid is None:
            continue
        canon = canonical_of.get(orig_lid, orig_lid)
        entry = by_team.setdefault(team_id, {"leagues": set(), "seasons": set()})
        entry["leagues"].add(canon)
        entry["seasons"].add(season_id)

    issues = []
    for team_id, entry in by_team.items():
        if len(entry["leagues"]) > 1 and team_id not in decided:
            league_ids = sorted(entry["leagues"])
            issues.append({
                "team_id": team_id,
                "team_name": team_name.get(team_id, team_id),
                "league_ids": league_ids,
                "league_names": [name_of.get(lid, lid) for lid in league_ids],
                "season_ids": sorted(entry["seasons"])})
    return sorted(issues, key=lambda x: x["team_id"])


def find_invalid_team_league_decisions(conn):
    """Operator decisions naming a ``league_id`` that is not an existing league —
    a typo or stale id that would strand the team on a nonexistent League."""
    cur = conn.cursor()
    cur.execute("SELECT id FROM leagues")
    league_ids = {r["id"] for r in cur.fetchall()}
    cur.execute("SELECT team_id, league_id FROM team_league_migration_decisions")
    return sorted(
        ({"team_id": r["team_id"], "league_id": r["league_id"]}
         for r in cur.fetchall() if r["league_id"] not in league_ids),
        key=lambda x: x["team_id"])


def find_league_season_merge_collisions(conn):
    """(program, key, season) groups holding more than one per-Season league row.

    Two league rows that merge to the same permanent League AND share a Season
    would produce a duplicate ``league_seasons(league_id, season_id)`` — the
    migration can't represent both, so the operator must merge/rename them
    first. Returns ``[{canonical_league_id, season_id, league_ids}, ...]``."""
    canonical_of, _name_of, season_of = _canonical_league_index(conn)
    groups = {}
    for lid, canon in canonical_of.items():
        groups.setdefault((canon, season_of[lid]), []).append(lid)
    return sorted(
        ({"canonical_league_id": canon, "season_id": season_id,
          "league_ids": sorted(members)}
         for (canon, season_id), members in groups.items() if len(members) > 1),
        key=lambda x: (x["canonical_league_id"], x["season_id"]))


def find_duplicate_team_league_seasons(conn):
    """(team, canonical league, season) triples with more than one registration —
    they would collide on the new ``UNIQUE(team_id, league_season_id)``."""
    canonical_of, _name_of, _season_of = _canonical_league_index(conn)
    effective = _effective_original_league(conn)
    seen = {}
    for team_id, season_id, orig_lid in effective.values():
        if orig_lid is None:
            continue
        key = (team_id, canonical_of.get(orig_lid, orig_lid), season_id)
        seen[key] = seen.get(key, 0) + 1
    return sorted(
        ({"team_id": t, "canonical_league_id": lg, "season_id": s, "count": n}
         for (t, lg, s), n in seen.items() if n > 1),
        key=lambda x: (x["team_id"], x["canonical_league_id"]))


def assert_competition_hierarchy_reset_ready(conn):
    """Abort migration 035 (#283 Slice A) unless the Program -> League -> Team
    reset is unambiguous and collision-free.

    Read-only: raises :class:`MigrationDataError` with bounded, row-level
    diagnostics and leaves all data unchanged, so an operator resolves each
    issue — most importantly by recording a permanent ``league_id`` in
    ``team_league_migration_decisions`` for every Team historically registered
    across multiple Leagues — and the migration re-runs and applies on the next
    startup. A Team resolving to a single League migrates automatically and is
    never reported.
    """
    ambiguous = find_ambiguous_team_leagues(conn)
    invalid = find_invalid_team_league_decisions(conn)
    collisions = find_league_season_merge_collisions(conn)
    dup_regs = find_duplicate_team_league_seasons(conn)
    if not (ambiguous or invalid or collisions or dup_regs):
        return

    parts = []
    if ambiguous:
        shown = "; ".join(
            f"team {i['team_id']} ({i['team_name']}) spans leagues "
            f"{', '.join(i['league_names'])} across seasons "
            f"{', '.join(i['season_ids'])}" for i in ambiguous[:20])
        more = "" if len(ambiguous) <= 20 else f" (+{len(ambiguous) - 20} more)"
        parts.append(
            f"{len(ambiguous)} team(s) are registered across multiple Leagues "
            "and need an operator-supplied permanent league_id in "
            f"team_league_migration_decisions: {shown}{more}")
    if invalid:
        shown = ", ".join(
            f"team {i['team_id']}->league {i['league_id']}" for i in invalid[:20])
        parts.append(f"{len(invalid)} operator decision(s) name a nonexistent "
                     f"league: {shown}")
    if collisions:
        shown = ", ".join(
            f"league {i['canonical_league_id']} in season {i['season_id']} "
            f"(from {', '.join(i['league_ids'])})" for i in collisions[:20])
        parts.append(f"{len(collisions)} merged League(s) collide within a "
                     f"single Season: {shown}")
    if dup_regs:
        shown = ", ".join(
            f"team {i['team_id']}/league {i['canonical_league_id']}/season "
            f"{i['season_id']} (x{i['count']})" for i in dup_regs[:20])
        parts.append(f"{len(dup_regs)} team(s) have duplicate registrations for "
                     f"the same League and Season: {shown}")
    raise MigrationDataError(
        "Cannot run the competition-hierarchy reset (#283 Slice A): "
        + "; ".join(parts) + ".")


# -- #283 Slice E — every regular Game must resolve to a LeagueSeason ----------
#
# Migration 037 stamps games.league_season_id from the unique (league_id,
# season_id) LeagueSeason. A REGULAR game that cannot resolve one (a null
# league/season, or a pair with no LeagueSeason row) would be left unscoped —
# which #283 forbids (only exhibitions may be unscoped). This gate runs BEFORE
# 037's ALTER (game_type from 036 and league_seasons from 035 already exist) and
# aborts the upgrade with a bounded row list so an operator resolves each game
# (set its league/season to an existing LeagueSeason, or mark it an exhibition)
# rather than migrating an invalid regular game. Pure SELECT, portable, re-runnable.

def find_unresolved_regular_games(conn):
    """Ids of REGULAR games that cannot be bound to a single, consistent
    LeagueSeason by migration 037:

      * their ``(league_id, season_id)`` is null on either side, or resolves to
        no ``league_seasons`` row; OR
      * they carry a ``division_id`` whose Division belongs to a DIFFERENT
        LeagueSeason than that pair (a cross-LeagueSeason Division/Game
        mismatch) — which would let a Game be counted in one Division's
        standings while its League/Season point elsewhere.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT g.id AS id FROM games g "
        "WHERE g.game_type = 'regular' "
        "AND (g.league_id IS NULL OR g.season_id IS NULL "
        "     OR NOT EXISTS (SELECT 1 FROM league_seasons ls "
        "                    WHERE ls.league_id = g.league_id "
        "                    AND ls.season_id = g.season_id) "
        "     OR (g.division_id IS NOT NULL AND NOT EXISTS ("
        "            SELECT 1 FROM divisions d "
        "            JOIN league_seasons ls2 ON ls2.id = d.league_season_id "
        "            WHERE d.id = g.division_id "
        "            AND ls2.league_id = g.league_id "
        "            AND ls2.season_id = g.season_id)))")
    return sorted(row["id"] for row in cur.fetchall())


def assert_regular_games_resolve_league_season(conn):
    """Abort migration 037 if any regular game can't be bound to a single,
    consistent LeagueSeason — an unresolvable (league, season) pair, or a
    Division that belongs to a different LeagueSeason (#283 Slice E)."""
    bad = find_unresolved_regular_games(conn)
    if bad:
        shown = ", ".join(bad[:20])
        more = "" if len(bad) <= 20 else f" (+{len(bad) - 20} more)"
        raise MigrationDataError(
            "Cannot bind regular games to a LeagueSeason (#283 Slice E): "
            f"{len(bad)} regular game(s) have no resolvable (league, season) "
            "LeagueSeason, or carry a Division from a different LeagueSeason: "
            f"{shown}{more}. Set each game's league, season, and division to a "
            "single existing LeagueSeason, or mark it an exhibition, before "
            "upgrading.")


# -- #269 — active-team jersey uniqueness ----------------------------------
#
# Migration 038 adds a partial UNIQUE index on players(team_id, jersey_number)
# WHERE is_active = 1 AND jersey_number IS NOT NULL. This read-only check reports
# every (team, jersey) that ALREADY has more than one active player before the
# index is created, so an upgrade against dirty data fails loudly with the
# offending pairs named. Only active, concrete-jersey rows are considered, so it
# matches the partial index exactly (NULL jerseys and inactive players are free
# to repeat). Pure SELECT, portable across SQLite/PostgreSQL, safe to re-run.


def find_duplicate_active_team_jerseys(conn):
    """Concrete (team_id, jersey_number) pairs worn by >1 ACTIVE player.

    Only ``is_active = 1`` rows with a non-null ``jersey_number`` are counted —
    matching migration 038's partial unique index, which excludes inactive
    players (they don't reserve a number) and NULL jerseys (a player may have
    none). Returns a sorted list of ``(team_id, jersey_number)`` tuples.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT team_id, jersey_number FROM players "
        "WHERE is_active = 1 AND jersey_number IS NOT NULL "
        "GROUP BY team_id, jersey_number HAVING COUNT(*) > 1")
    return sorted((row["team_id"], row["jersey_number"])
                  for row in cur.fetchall())


def assert_no_duplicate_active_team_jerseys(conn):
    """Abort migration 038 if any team has two active players sharing a jersey
    number (#269)."""
    duplicates = find_duplicate_active_team_jerseys(conn)
    if duplicates:
        pairs = [f"team {team}/#{jersey}" for team, jersey in duplicates[:20]]
        more = "" if len(duplicates) <= 20 else f" (+{len(duplicates) - 20} more)"
        raise MigrationDataError(
            "Cannot enforce one active player per jersey number per team: "
            f"{len(duplicates)} (team, jersey) pair(s) already have multiple "
            f"active players: {', '.join(pairs)}{more}. Reassign or deactivate "
            "the duplicates before upgrading.")


def find_invalid_player_jersey_numbers(conn):
    """Existing non-null jerseys outside the canonical ``1..98`` range.

    Range validity applies to active and inactive Players alike; inactivity
    releases uniqueness but does not make an invalid stored number valid.
    Returns actionable ``(player_id, team_id, jersey_number, is_active)`` rows.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT id, team_id, jersey_number, is_active FROM players "
        "WHERE jersey_number IS NOT NULL "
        f"AND (jersey_number < {MIN_JERSEY_NUMBER} "
        f"OR jersey_number > {MAX_JERSEY_NUMBER}) ORDER BY id")
    return [(row["id"], row["team_id"], row["jersey_number"],
             bool(row["is_active"])) for row in cur.fetchall()]


def assert_player_jersey_constraints_ready(conn):
    """Abort migration 038 unless existing data satisfies both jersey rules."""
    invalid = find_invalid_player_jersey_numbers(conn)
    duplicates = find_duplicate_active_team_jerseys(conn)
    if not invalid and not duplicates:
        return

    problems = []
    if invalid:
        shown = [
            f"player {player} on team {team}/#{jersey} "
            f"({'active' if active else 'inactive'})"
            for player, team, jersey, active in invalid[:20]]
        more = "" if len(invalid) <= 20 else f" (+{len(invalid) - 20} more)"
        problems.append(
            f"{len(invalid)} player(s) have a non-null jersey outside "
            f"{MIN_JERSEY_NUMBER}..{MAX_JERSEY_NUMBER}: "
            f"{', '.join(shown)}{more}")
    if duplicates:
        pairs = [f"team {team}/#{jersey}" for team, jersey in duplicates[:20]]
        more = ("" if len(duplicates) <= 20
                else f" (+{len(duplicates) - 20} more)")
        problems.append(
            f"{len(duplicates)} active (team, jersey) pair(s) are duplicated: "
            f"{', '.join(pairs)}{more}")
    raise MigrationDataError(
        "Cannot enforce Player jersey constraints: " + "; ".join(problems)
        + ". Reassign a valid jersey or deactivate/reassign duplicate active "
          "players before upgrading.")
