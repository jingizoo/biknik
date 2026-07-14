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
