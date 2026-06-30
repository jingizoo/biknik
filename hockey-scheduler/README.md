# Hockey Scheduler

iPhone-first ice hockey league scheduling app for junior and senior leagues.

This repository currently contains the **first vertical slice**: game roster
management with a position-aware substitute workflow. The slice proves the
single most important product behavior:

> When a selected player backs out, the system immediately tells the coach
> whether a substitute is available or whether the game now has an open slot —
> counting goalie and skater slots separately.

## What is in this slice

| Area | Status |
| --- | --- |
| Domain model (Game, Player, roster, availability, substitutes, audit) | ✅ implemented |
| Roster-status engine (open goalie/skater slot calculation) | ✅ implemented |
| Substitute workflow (enroll → offer → accept / decline / withdraw) | ✅ implemented |
| Coach override (manual add, remove, lock roster) | ✅ implemented |
| Audit trail for every state change | ✅ implemented |
| Notification events (defined + emitted from the service) | ✅ implemented |
| Service facade mapping to the REST API contract | ✅ implemented |
| League + Arena setup (league/season/division, club/team, venue/rink/ice-slot, manual game) | ✅ implemented |
| Persistence: in-memory + SQL store (PostgreSQL target, SQLite dev adapter) | ✅ implemented |
| Browser-based iPhone-framed demo UI (Coach + Player views) | ✅ implemented |
| Native iOS app (SwiftUI) | ⛔ follow-up — needs a Mac/Xcode; screens are specified in `docs/` |
| Season scheduler engine | ⛔ explicitly out of scope |

The backend is written in pure-stdlib Python so it runs and tests with no
third-party dependencies. The persistence layer is an in-memory store seeded
with mock data, exactly as the kickoff issue allows ("mock/local data if
backend is not ready").

## Layout

```text
hockey-scheduler/
  README.md
  CLAUDE.md
  docs/
    product/      mvp-scope.md, substitute-use-case.md
    architecture/ data-model.md, api-contract.md
  backend/
    hockey_scheduler/
      domain/     enums, models, errors
      services/   roster_service (status engine + workflow)
      store/      in-memory repository
      api/        service facade mapping to the API contract
      web/        iPhone-framed E2E demo: stdlib HTTP server + 7-tab UI
      seed.py     single-game mock data builder
      full_demo.py  full Alpine league/arena E2E seed (real setup service)
    tests/        unittest suite (no external deps)
```

## Web operator console (no Mac needed)

A native iOS app needs a Mac + Xcode, so until then the app ships as a
**responsive web console** that drives the *real* backend through the
documented API. It runs on any OS with just Python — no third-party packages.

```bash
cd hockey-scheduler/backend
python3 -m hockey_scheduler.web        # then open http://localhost:8000
```

- **`/`** — the desktop **Operator Console**: a sidebar app (Dashboard, Setup,
  Arena Calendar, Games, Roster, Activity, Public) with a wide layout, designed
  for a league/arena operator. Collapses to a top nav on narrow screens.
- **`/mobile`** — the original **iPhone-framed preview** of the same flows
  (the design reference for the future native app).

Both surfaces share the same `app.js` and the same API; the desktop console is
just a different shell over the same views.

It is a **calendar-first operator demo** seeded (via the real setup service)
with the Alpine Ice Hockey League scenario. Every create and schedule action
hits the API and writes real store records. The bottom tab bar is organized as
an operator flow:

- **Dashboard** — league summary, live counts (games, available ice, confirmed
  rosters, officials), and primary actions.
- **Setup** — real forms to **create** a league, season, division, club, team,
  venue, and rink (POST to the setup API).
- **Calendar** — an arena board (rinks as rows, ice slots as time-ordered
  cards). Tap an **Available** slot to open the **Schedule Game wizard**
  (division → teams → ice → live validation → *Create Game*). *+ Add ice* adds
  a slot to a rink.
- **Games** — every scheduled game with a **Game Operations** checklist (ice
  allocated, roster status, officials #30, locker rooms, scorekeeper #31,
  public fixture) and *Open Roster*.
- **Roster** — the Coach/Player substitute flow for the selected game (mark
  *Can't play* / *Re-confirm*, position-aware *Add*, *Lock Roster*; locked
  state disables player actions).
- **Activity** — the live notification feed and audit trail.
- **Public** — the fan fixture page. Shows fixtures only — **no junior player
  names or any personal/guardian/medical data**.

Suggested walkthrough: **Setup** create a club; **Calendar** tap an available
slot and run the **Schedule Game** wizard → the game appears under **Games**;
*Open Roster* → on **Coach** mark a skater *Can't play* → **Open Slot**; on
**Player** *Enroll as Substitute* → **Needs Substitute Decision**; back on
**Coach** *Add* → **Roster Confirmed** → *Lock Roster*. **Public** shows the
safe fixture. *Reset* restores the starting state.

## Run the tests

```bash
cd hockey-scheduler/backend
python3 -m unittest discover -s tests -v
```

## Try it

```bash
cd hockey-scheduler/backend
python3 -m hockey_scheduler.demo
```

The demo seeds a game, has a confirmed skater back out, and prints the
recalculated roster status — first with no substitutes enrolled (Open Slot),
then with a substitute enrolled and added to the roster (slot closed).

## League + Arena setup demo

```bash
cd hockey-scheduler/backend
python3 -m hockey_scheduler.setup_demo
```

Builds a league → season → division, two clubs/teams, and a venue → rink →
ice slot, then creates a manual game on that slot (rejecting a double-booking)
and shows the new game driving the existing roster engine. See
`docs/architecture/league-arena-setup.md`.
