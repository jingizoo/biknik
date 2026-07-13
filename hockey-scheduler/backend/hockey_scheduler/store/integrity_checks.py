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
