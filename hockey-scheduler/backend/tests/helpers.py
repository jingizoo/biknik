"""Shared test helpers: a deterministic clock and a small game builder."""

import os
import sys
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path

# Speed up the suite: PBKDF2 password hashing is deliberately expensive
# (~80 ms/call in production), and the suite makes hundreds of hash/verify
# calls. Lower the iteration count for tests ONLY — set here, before any
# hockey_scheduler import so passwords.py reads it at module load. Production
# never sets this var, so it keeps the strong default. setdefault so an
# explicit override still wins.
os.environ.setdefault("HS_PBKDF2_ITERATIONS", "1000")

# Make ``hockey_scheduler`` importable when tests run from any directory.
BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def suspend_program_org_fks(store):
    """Disable migration 042's Program/Organization + Venue-owner foreign keys on
    a test store so legacy *dangling* rows can be planted — a program/season/
    league/venue whose owner id resolves to no row. Those are the pre-042 data
    older migration/preflight/legacy-field tests deliberately model, which the new
    constraints would now reject.

    PostgreSQL drops the five named constraints. SQLite disables enforcement on
    the connection (its inline column-level foreign keys can't be dropped without
    a full table rebuild, and these tests either only read afterward or re-run
    ``migrate()``, which manages the pragma itself). A no-op on the in-memory
    store, which has no foreign keys. Idempotent and safe to call at setup time.
    """
    from hockey_scheduler.store import SqlStore
    if not isinstance(store, SqlStore):
        return
    if store.backend == "postgres":
        cur = store.conn.cursor()
        for table, constraint in (
                ("programs", "fk_programs_operator_org"),
                ("venues", "fk_venues_organization"),
                ("seasons", "fk_seasons_program"),
                ("leagues", "fk_leagues_program"),
                ("venues", "fk_venues_program")):
            cur.execute(
                f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {constraint}")
    else:
        store.conn.execute("PRAGMA foreign_keys = OFF")


def fresh_sql_store(url):
    """A migrated, EMPTY ``SqlStore`` on ``url``, tolerant of whatever a
    previously-run test module left in a shared database (#369).

    ``run_parallel.py`` gives each worker its own PostgreSQL database but runs
    that worker's modules SERIALLY against it, so a module inherits the last
    one's leftovers. The obvious ``SqlStore(url).clear_all_data()`` cannot
    always recover: ``SqlStore.__init__`` runs ``migrate()`` FIRST, and some
    modules deliberately plant rows that a later migration's integrity check
    rejects — e.g. ``test_iceslot_venue_fks`` seeds rinks/slots/games/access
    rows pointing at parents that do not exist, which makes
    ``assert_iceslot_venue_fks_ready`` raise ``MigrationDataError`` before
    ``clear_all_data`` is ever reachable. The module never cleans up after
    itself, so every module scheduled behind it in the same worker inherits a
    database it cannot even open.

    That hazard is ordering-dependent and therefore invisible until module
    sharding shifts: ``test_active_context_league`` has used the plain
    construct-then-clear idiom safely only because round-robin sharding happens
    to place it first in its worker. Adding modules re-shards everything, so
    this helper makes the recovery explicit instead of leaving it to luck.

    Drops the schema outright when a plain open fails, then re-migrates — so
    the caller always gets a clean, fully-migrated store regardless of what
    came before.
    """
    from hockey_scheduler.store import SqlStore
    from hockey_scheduler.store.db import connect
    try:
        store = SqlStore(url)
    except Exception:
        # Unopenable: tear the schema down at the raw-connection level (no
        # SqlStore can be constructed to do it for us) and re-migrate.
        conn, dialect, _path = connect(url)
        cur = conn.cursor()
        if dialect.backend == "postgres":
            cur.execute("DROP SCHEMA public CASCADE")
            cur.execute("CREATE SCHEMA public")
        else:
            cur.execute("PRAGMA writable_schema = ON")
            for (name,) in cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                cur.execute(f'DROP TABLE IF EXISTS "{name}"')
            cur.execute("PRAGMA writable_schema = OFF")
        conn.commit()
        conn.close()
        store = SqlStore(url)
    store.clear_all_data()
    return store


def cookie_from_set_cookie(set_cookie_header, name):
    """Extract a single cookie's ``name=value`` from a Set-Cookie header.

    Used by production HTTP tests to propagate the session cookie manually: in
    production the cookie is issued with ``Secure`` (#76), which a real client
    (and Python's CookieJar) will not send back over plain-HTTP loopback. The
    server issued it correctly — the test just replays it explicitly so we can
    exercise authenticated behavior without pretending the transport is HTTPS.
    """
    if not set_cookie_header:
        return None
    jar = SimpleCookie()
    jar.load(set_cookie_header)
    morsel = jar.get(name)
    return f"{name}={morsel.value}" if morsel else None


def commit_fresh_draft(api, division_id=None, *, season_id=None,
                       league_id=None, slot_ids=None, constraints=None,
                       actor_id=None):
    """Preview-then-commit convenience for tests that don't care about
    staleness detection (#328 review round 5 made ``draft_fingerprint`` a
    required, server-validated preview-binding token on
    ``commit_draft_schedule``, mirroring the ice-availability builder's
    ``template_fingerprint`` on ``commit_ice_availability``): fetches a
    fresh proposal with the same scope and passes its fingerprint straight
    through, so ordinary "just commit and check the result" tests don't
    need to reproduce that boilerplate at every call site. Tests that
    specifically exercise a stale or mismatched fingerprint call
    ``commit_draft_schedule`` directly instead."""
    proposal = api.draft_season_schedule(
        division_id=division_id, season_id=season_id, league_id=league_id,
        slot_ids=slot_ids, constraints=constraints)
    if isinstance(proposal, dict) and proposal.get("error"):
        return proposal
    return api.commit_draft_schedule(
        division_id=division_id, season_id=season_id, league_id=league_id,
        slot_ids=slot_ids, constraints=constraints,
        draft_fingerprint=proposal.get("draft_fingerprint"),
        actor_id=actor_id)

from hockey_scheduler.domain import Game, Player, Position, Team  # noqa: E402
from hockey_scheduler.services import RosterService  # noqa: E402
from hockey_scheduler.store import InMemoryStore  # noqa: E402


class FakeClock:
    """Monotonic, deterministic clock for reproducible timestamps."""

    def __init__(self) -> None:
        self._t = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self._t += timedelta(seconds=1)
        return self._t


def make_service(target_goalies: int = 1, target_skaters: int = 4):
    """Build a service with one team, a goalie + skaters, and one game.

    Returns (service, store, game_id). Players created:
      - player_goalie_1, player_goalie_2 (goalies)
      - player_skater_1 .. player_skater_8 (skaters)
    """
    store = InMemoryStore()
    team = Team(id="team_1", name="Test Team", division="U16")
    store.add_team(team)

    store.add_player(Player(id="player_goalie_1", team_id=team.id,
                            name="Goalie One", position=Position.GOALIE))
    store.add_player(Player(id="player_goalie_2", team_id=team.id,
                            name="Goalie Two", position=Position.GOALIE))
    for i in range(1, 9):
        pos = Position.DEFENSE if i % 2 == 0 else Position.FORWARD
        store.add_player(Player(id=f"player_skater_{i}", team_id=team.id,
                                name=f"Skater {i}", position=pos))

    game = Game(
        id="game_1",
        home_team_id=team.id,
        start_time=datetime(2026, 2, 1, 18, 30, tzinfo=timezone.utc),
        target_goalies=target_goalies,
        target_skaters=target_skaters,
        max_skaters=target_skaters + 3,
    )
    store.add_game(game)

    service = RosterService(store, clock=FakeClock())
    return service, store, game.id


def select_and_confirm(service, game_id, player_ids, coach="coach_1"):
    """Select the given players and mark them all confirmed."""
    service.select_roster(game_id, player_ids, actor_id=coach)
    from hockey_scheduler.domain import AvailabilityStatus

    for pid in player_ids:
        service.set_availability(game_id, pid, AvailabilityStatus.AVAILABLE)
