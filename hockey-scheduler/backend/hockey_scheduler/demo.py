"""Runnable demo of the primary success behavior.

    python3 -m hockey_scheduler.demo

Seeds a game, selects + confirms a roster, then has a confirmed skater back
out. It prints the recalculated roster status first with no substitutes
enrolled (Open Slot), then after a substitute enrolls and is added (slot
closed).
"""

from .api import ApiService
from .seed import build_seeded_store


def _print_status(api: ApiService, game_id: str, label: str) -> None:
    status = api.get_roster_status(game_id)
    print(f"\n=== {label} ===")
    print(f"  status:        {status['status']}")
    print(f"  goalies:       {status['confirmed_goalies']}/{status['target_goalies']} "
          f"(open {status['open_goalie_slots']})")
    print(f"  skaters:       {status['confirmed_skaters']}/{status['target_skaters']} "
          f"(open {status['open_skater_slots']})")
    print(f"  subs enrolled: {status['substitutes_enrolled']}")
    print(f"  action req'd:  {status['action_required']}")
    print(f"  message:       {status['message']}")


def main() -> None:
    store, game_id = build_seeded_store()
    api = ApiService(store)
    coach = "coach_1"

    # Select 1 goalie + 15 skaters, then everyone confirms.
    selected = ["player_goalie_1"] + [f"player_skater_{i}" for i in range(1, 16)]
    api.select_roster(game_id, selected, actor_id=coach)
    for pid in selected:
        api.set_availability(game_id, pid, "available")
    _print_status(api, game_id, "Roster confirmed")

    # A confirmed skater backs out — no substitutes enrolled yet.
    api.set_availability(game_id, "player_skater_5", "unavailable")
    _print_status(api, game_id, "Skater backed out, no substitutes")

    # An eligible non-selected player enrolls as a substitute.
    api.enroll_substitute(game_id, "player_skater_16", actor_id="player_skater_16")
    _print_status(api, game_id, "Substitute enrolled")

    # Coach adds the substitute to the roster (override) — slot closes.
    api.add_substitute_to_roster(game_id, "player_skater_16", actor_id=coach)
    _print_status(api, game_id, "Substitute added to roster")

    # Show the notification + audit trail that was produced.
    print("\n=== Notifications emitted ===")
    for n in store.notifications_for_game(game_id):
        print(f"  [{n.audience}] {n.type.value}: {n.message}")
    print(f"\n=== Audit entries: {len(store.audit_for_game(game_id))} ===")


if __name__ == "__main__":
    main()
