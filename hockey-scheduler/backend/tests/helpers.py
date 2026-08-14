"""Shared test helpers: a deterministic clock and a small game builder."""

import os
import re
import sys
import tempfile
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


_PG_WORKER_DB_SUFFIX = re.compile(r"_p\d+$")


def _assert_disposable_test_database(url):
    """Hard-assert that ``url`` unambiguously names a disposable, per-worker
    test database — the one fact that must be PROVEN, not assumed, before
    ``fresh_sql_store`` is allowed to drop anything (#369 follow-up review:
    the original recovery path caught bare ``Exception`` and dropped
    unconditionally, which could just as easily erase a real database as the
    one known poisoned fixture).

    Pure string-parsing — no I/O, no live connection — so it is exercised the
    same way with or without a real PostgreSQL/SQLite database behind it:

    * PostgreSQL (``postgres://`` / ``postgresql://``): ``run_parallel.py``
      hands every worker its OWN database, named by suffixing the base
      database with ``_p<N>`` (see ``_pg_db_url``/``_ensure_pg_database``).
      That exact suffix is the only guarantee anywhere in the suite that a
      Postgres URL names a private per-worker throwaway rather than a
      developer's real or shared database, so it is required verbatim.
    * SQLite: only the literal ``:memory:`` database (private to one
      connection, gone the instant it closes) or a file that lives inside the
      OS temp directory (``tempfile.gettempdir()`` — exactly where this
      suite's own ``tempfile.mkstemp()`` fixtures live, and nowhere a real
      project database would ever be pointed) counts as disposable.

    Anything else raises ``AssertionError`` — never guesses, never drops.
    """
    if url.startswith(("postgres://", "postgresql://")):
        dbname = url.split("?", 1)[0].rsplit("/", 1)[-1]
        if _PG_WORKER_DB_SUFFIX.search(dbname):
            return
        raise AssertionError(
            f"refusing to drop schema on postgres database {dbname!r}: its "
            "name does not carry the per-worker '_p<N>' suffix run_parallel.py "
            "gives every worker's disposable test database, so it cannot be "
            "proven to be a throwaway. Aborting rather than guessing.")
    if url == ":memory:":
        return
    tmp_root = os.path.realpath(tempfile.gettempdir())
    if os.path.commonpath([tmp_root, os.path.realpath(url)]) == tmp_root:
        return
    raise AssertionError(
        f"refusing to drop schema on sqlite database {url!r}: it is neither "
        "the private ':memory:' database nor a file under the OS temp "
        "directory, so it cannot be proven to be a disposable test database. "
        "Aborting rather than guessing.")


def fresh_sql_store(url):
    """A migrated, EMPTY ``SqlStore`` on ``url``, tolerant of the ONE known,
    deliberately-poisoned condition a previous test module in this same
    worker can leave behind (#369): ``test_iceslot_venue_fks`` downgrades
    migration 041 to plant rinks/ice-slots/games/season-venue-access rows
    whose parents do not exist, which makes ``assert_iceslot_venue_fks_ready``
    raise ``MigrationDataError`` on the very next ``SqlStore(url)`` — before
    ``clear_all_data`` is ever reachable — for any module scheduled behind it
    in the same worker (``run_parallel.py`` gives each worker its own
    PostgreSQL database but runs that worker's modules SERIALLY against it).

    A prior version of this helper caught bare ``Exception`` and dropped the
    schema unconditionally, on ANY open failure. That is exactly the
    destructive, non-falsifiable pattern a repo-owner review flagged (#369
    follow-up): a migration regression that fails only against existing data
    would raise on the first open, get silently erased here, and then PASS on
    the automatic retry against an empty database — hiding the very
    regression the test exists to catch. This version narrows the catch to
    the *exact* intentional poison — ``MigrationDataError``, imported from its
    real module, never a bare ``Exception`` — and additionally hard-asserts
    (see ``_assert_disposable_test_database``) that ``url`` names a provably
    disposable per-worker database before it drops anything. Any OTHER
    exception, or any URL that isn't provably disposable, propagates
    UNCAUGHT, with the schema and data left exactly as they were.
    """
    from hockey_scheduler.store import SqlStore
    from hockey_scheduler.store.db import connect
    from hockey_scheduler.store.integrity_checks import MigrationDataError
    try:
        store = SqlStore(url)
    except MigrationDataError:
        _assert_disposable_test_database(url)
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


def end_membership_directly(store, membership_id, status="released"):
    """Move a SeasonRosterMembership straight to a TERMINAL status via the
    STORE layer, bypassing ``SetupService.set_season_roster_membership_
    status`` entirely.

    #205 review round 2 (owner product ruling, overriding round 1 finding
    5's shipped "actor_id + reason" floor): that service method now hard-
    refuses EVERY terminal transition (released/transferred), for any
    actor_id/reason, unconditionally — no caller can reach one through it
    any more. Several existing Slice-A tests need an ALREADY-terminal
    membership only as a PRECONDITION for something ELSE they exercise
    (e.g. "a terminal membership does not block unregister/transfer",
    unlike a live one; "release frees a jersey number") — not to test the
    transition method's own authorization, which is exactly what the owner
    ruling says must be reconstructed this way rather than weakened back
    open. A membership moved this way carries no ``status_changed`` event
    and no audit row (a direct write, not a service call) — callers whose
    assertions count events/audits need to account for that.
    """
    from hockey_scheduler.domain import MembershipStatus
    m = store.get_season_roster_membership(membership_id)
    m.status = MembershipStatus(status)
    store.save_season_roster_membership(m)
    return m


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
                       meetings_per_opponent=None, games_per_team=None,
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
    ``commit_draft_schedule`` directly instead.

    ``games_per_team``/``meetings_per_opponent`` (#375) are passed to BOTH
    calls, which is the contract the real Scheduler UI has to honour too:
    the format is bound into ``draft_fingerprint``, so previewing one format
    and committing another is refused as ``preview_stale`` rather than
    silently committing a differently-sized schedule."""
    proposal = api.draft_season_schedule(
        division_id=division_id, season_id=season_id, league_id=league_id,
        slot_ids=slot_ids, constraints=constraints,
        meetings_per_opponent=meetings_per_opponent,
        games_per_team=games_per_team)
    if isinstance(proposal, dict) and proposal.get("error"):
        return proposal
    return api.commit_draft_schedule(
        division_id=division_id, season_id=season_id, league_id=league_id,
        slot_ids=slot_ids, constraints=constraints,
        draft_fingerprint=proposal.get("draft_fingerprint"),
        meetings_per_opponent=meetings_per_opponent,
        games_per_team=games_per_team,
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
