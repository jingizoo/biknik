"""One-shot fixture alignment for #173 PR C. Removed before merge."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "backend" / "tests"


def replace(path, old, new, count=None):
    p = ROOT / path
    text = p.read_text()
    actual = text.count(old)
    expected = count if count is not None else 1
    if actual != expected:
        raise RuntimeError(f"{path}: expected {expected} occurrences, found {actual}: {old!r}")
    p.write_text(text.replace(old, new))


replace(
    "test_draft_review.py",
    "    Division, Game, IceSlot, IceSlotStatus, Official, Rink, Team)",
    "    Division, Game, IceSlot, IceSlotStatus, League, Official, Organization,\n"
    "    Rink, Season, Team, Venue)",
)
replace(
    "test_draft_review.py",
    '    s = InMemoryStore()\n'
    '    s.add_division(Division(id="d", season_id="se", name="D1"))\n'
    '    s.add_rink(Rink(id="r1", venue_id="v", name="Main"))',
    '    s = InMemoryStore()\n'
    '    s.add_organization(Organization(id="org", name="Owner"))\n'
    '    s.add_league(League(id="league", name="League", organization_id="org"))\n'
    '    s.add_season(Season(id="se", league_id="league", name="Season"))\n'
    '    s.add_division(Division(id="d", season_id="se", name="D1"))\n'
    '    s.add_venue(Venue(id="v", name="Arena", organization_id="org",\n'
    '                      league_id="league"))\n'
    '    s.add_rink(Rink(id="r1", venue_id="v", name="Main"))',
)

replace(
    "test_guardian_notification_delivery.py",
    '    venue = api.setup.create_venue("Arena")',
    '    venue = api.setup.create_venue("Arena", league_id=league.id)',
)

import_anchor = '        league = self.setup.create_league("Test League", actor_id="admin")\n'
import_replacement = (
    import_anchor
    + '        venue = self.store.get_venue(self._rink("R1").venue_id)\n'
    + '        self.setup.assign_venue_league(venue.id, league.id, actor_id="admin")\n'
)
replace(
    "test_import_rinks_ice_slots_commit.py",
    import_anchor,
    import_replacement,
    count=2,
)

replace(
    "test_league_arena_setup.py",
    '    venue = svc.create_venue("Ice Palace")',
    '    venue = svc.create_venue("Ice Palace", league_id=league.id)',
)
replace(
    "test_league_arena_setup.py",
    '        rink = svc.create_rink(svc.create_venue("VE").id, "RI")',
    '        rink = svc.create_rink(\n'
    '            svc.create_venue("VE", league_id=lg.id).id, "RI")',
)

replace(
    "test_officials.py",
    '        venue = self.svc.create_venue("Arena")',
    '        venue = self.svc.create_venue("Arena", league_id=league.id)',
)
replace(
    "test_reschedule_workflow.py",
    '    venue = svc.create_venue("Arena")',
    '    venue = svc.create_venue("Arena", league_id=league.id)',
)

replace(
    "test_scheduling_constraints.py",
    'from hockey_scheduler.domain import Division, IceSlot, Rink, Team',
    'from hockey_scheduler.domain import (\n'
    '    Division, IceSlot, League, Organization, Rink, Season, Team, Venue)',
)
replace(
    "test_scheduling_constraints.py",
    '    s = InMemoryStore()\n'
    '    s.add_division(Division(id="d", season_id="se", name="D"))\n'
    '    s.add_rink(Rink(id="r1", venue_id="v", name="Main"))',
    '    s = InMemoryStore()\n'
    '    s.add_organization(Organization(id="org", name="Owner"))\n'
    '    s.add_league(League(id="league", name="League", organization_id="org"))\n'
    '    s.add_season(Season(id="se", league_id="league", name="Season"))\n'
    '    s.add_division(Division(id="d", season_id="se", name="D"))\n'
    '    s.add_venue(Venue(id="v", name="Arena", organization_id="org",\n'
    '                      league_id="league"))\n'
    '    s.add_rink(Rink(id="r1", venue_id="v", name="Main"))',
)

replace(
    "test_setup_facade.py",
    '        venue = self.api.create_venue("Ice Palace")',
    '        venue = self.api.create_venue("Ice Palace", league_id=league["id"])',
)
replace(
    "test_sql_store.py",
    '        rink = svc.create_rink(svc.create_venue("V").id, "R")',
    '        rink = svc.create_rink(\n'
    '            svc.create_venue("V", league_id=league.id).id, "R")',
)

print("PR #177 legacy fixtures aligned with league-owned venues.")
