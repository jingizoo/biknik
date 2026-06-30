"""Runnable demo of the League + Arena setup flow.

    python3 -m hockey_scheduler.setup_demo

Builds a league/season/division, two clubs/teams, a venue/rink/ice slot, then
creates a manual game on that slot and shows it driving the existing roster
engine. Also demonstrates the duplicate-slot conflict.
"""

from datetime import datetime, timezone

from .api import ApiService


def main() -> None:
    api = ApiService()
    actor = "league_admin"

    league = api.create_league("EU Premier Hockey", country="DE", actor_id=actor)
    season = api.create_season(league["id"], "2026/27", actor_id=actor)
    division = api.create_division(season["id"], "U16", age_group="U16", actor_id=actor)

    club_a = api.create_club("Lions Club", actor_id=actor)
    club_b = api.create_club("Falcons Club", actor_id=actor)
    home = api.create_team(club_a["id"], division["id"], "U16 Lions", actor_id=actor)
    away = api.create_team(club_b["id"], division["id"], "U16 Falcons", actor_id=actor)

    venue = api.create_venue("Ice Palace", actor_id=actor)
    rink = api.create_rink(venue["id"], "Rink 2", actor_id=actor)
    slot = api.create_ice_slot(
        rink["id"], "2026-09-05T18:30:00+00:00", "2026-09-05T20:00:00+00:00",
        actor_id=actor,
    )

    print("=== Setup ===")
    print(f"  League:   {league['name']} ({league['id']})")
    print(f"  Season:   {season['name']}")
    print(f"  Division: {division['name']}")
    print(f"  Teams:    {home['name']} vs {away['name']}")
    print(f"  Ice slot: {rink['name']} @ {slot['start_time']} → {slot['end_time']}")

    game = api.create_game(season["id"], division["id"], home["id"], away["id"],
                           slot["id"], actor_id=actor)
    print("\n=== Manual game created ===")
    print(f"  {game['id']} · {game['rink']} · starts {game['start_time']}")
    print(f"  season={game['season_id']} division={game['division_id']} "
          f"slot={game['ice_slot_id']}")

    dup = api.create_game(season["id"], division["id"], home["id"], away["id"],
                          slot["id"], actor_id=actor)
    print("\n=== Double-booking the same slot is rejected ===")
    print(f"  {dup['error']['code']}: {dup['error']['message']}")

    # The new game immediately works with the roster engine.
    g1 = api.create_player(home["id"], "Goalie Gabe", "goalie", actor_id=actor)
    api.create_player(home["id"], "Aarav M.", "forward", actor_id=actor)
    api.select_roster(game["id"], [g1["id"]], actor_id="coach_1")
    status = api.get_roster_status(game["id"])
    print("\n=== Roster engine on the new game ===")
    print(f"  status={status['status']} · {status['message']}")


if __name__ == "__main__":
    main()
