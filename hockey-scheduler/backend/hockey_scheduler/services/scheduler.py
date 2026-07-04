"""Season scheduler engine v1 — draft fixture generation (#84).

A deliberately simple, deterministic generator: single round-robin pairings for
a division's teams, each pairing assigned to the earliest available game ice
slot. It produces a *draft* proposal only — nothing is persisted or published
here; review and publish are a later slice (#86). Pure over the store so the
output is reproducible and unit-testable.
"""

from ..domain import IceSlotStatus, IceSlotType


def round_robin_pairings(team_ids):
    """Single round-robin pairings via the circle method (deterministic).

    Returns a flat list of ``(home_team_id, away_team_id)`` in round order.
    Every team plays every other exactly once. An odd number of teams yields a
    bye each round (the byed team simply has no game that round). Home/away
    alternates by round for basic balance.
    """
    teams = sorted(team_ids)
    if len(teams) < 2:
        return []
    if len(teams) % 2 == 1:
        teams = teams + [None]  # None marks the bye
    n = len(teams)
    fixed, rot = teams[0], teams[1:]
    pairings = []
    for r in range(n - 1):
        arrangement = [fixed] + rot
        for i in range(n // 2):
            a, b = arrangement[i], arrangement[n - 1 - i]
            if a is None or b is None:
                continue  # bye
            pairings.append((a, b) if r % 2 == 0 else (b, a))
        rot = [rot[-1]] + rot[:-1]  # rotate all but the fixed team
    return pairings


def _available_game_slots(store, slot_ids=None):
    """Game-type ice slots that are AVAILABLE and not already tied to a game,
    earliest first (deterministic tie-break on id)."""
    wanted = set(slot_ids) if slot_ids else None
    slots = []
    for s in store.all_ice_slots():
        if wanted is not None and s.id not in wanted:
            continue
        if s.slot_type != IceSlotType.GAME or s.status != IceSlotStatus.AVAILABLE:
            continue
        if store.game_using_ice_slot(s.id) is not None:
            continue
        slots.append(s)
    slots.sort(key=lambda s: (s.start_time, s.id))
    return slots


def draft_schedule(store, division_id, slot_ids=None):
    """Generate a draft round-robin schedule for a division (#84).

    Returns ``{division_id, draft_games, unscheduled}``. Each draft game names
    the pairing and the assigned slot; a pairing with no slot left is returned
    in ``unscheduled`` with a reason. Nothing is persisted or published.
    """
    teams = sorted(t.id for t in store.all_teams()
                   if t.division_id == division_id)
    pairings = round_robin_pairings(teams)
    slots = _available_game_slots(store, slot_ids)

    def team_name(tid):
        t = store.get_team(tid)
        return t.name if t else tid

    draft_games, unscheduled = [], []
    slot_i = 0
    for home, away in pairings:
        if slot_i < len(slots):
            s = slots[slot_i]
            slot_i += 1
            rink = store.get_rink(s.rink_id) if s.rink_id else None
            draft_games.append({
                "home_team_id": home, "away_team_id": away,
                "home_team_name": team_name(home), "away_team_name": team_name(away),
                "division_id": division_id,
                "ice_slot_id": s.id,
                "rink_id": s.rink_id,
                "rink_name": rink.name if rink else None,
                "start_time": s.start_time.isoformat(),
                "end_time": s.end_time.isoformat(),
            })
        else:
            unscheduled.append({
                "home_team_id": home, "away_team_id": away,
                "home_team_name": team_name(home), "away_team_name": team_name(away),
                "division_id": division_id,
                "reason": "No available ice slot for this pairing.",
            })
    return {"division_id": division_id, "team_count": len(teams),
            "draft_games": draft_games, "unscheduled": unscheduled}
