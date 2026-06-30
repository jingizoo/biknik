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

## iPhone-framed web demo (no Mac needed)

A native iOS simulator only runs inside Xcode on a Mac. Until that follow-up,
this slice ships a **browser preview** that renders an iPhone frame and drives
the *real* roster/substitute engine through the documented API. It runs on any
OS (Windows included) with just Python — no third-party packages.

```bash
cd hockey-scheduler/backend
python3 -m hockey_scheduler.web        # then open http://localhost:8000
```

It is a **full end-to-end demo** seeded (via the real setup service) with the
Alpine Ice Hockey League scenario — league → season → divisions, clubs/teams,
venue → rinks → ice slots, and a manual U16 Lions vs U16 Falcons game. The
bottom tab bar has seven screens:

- **Today** — next game at a glance: hero card, goalie/skater fill bars, action
  items, and the follow-up modules (officials/results/notifications/calendar)
  shown as disabled cards linking to their issues.
- **League** — the seeded league, season, divisions (with Junior tags), clubs,
  and teams.
- **Arena** — venue, rinks, and ice slots (the 18:30 slot allocated to the
  game); a live *+ Add ice slot* action exercises the setup API.
- **Schedule** — the manual game created from the setup layer; *Open* jumps to
  the Game tab.
- **Game** — full Game Detail with a **Coach / Player** toggle (mark *Can't
  play* / *Re-confirm*, substitute pool with position-aware *Add*, *Lock
  Roster*; locked state disables player actions).
- **Activity** — the live notification feed and the audit trail.
- **Public** — the fan fixture page. Shows fixtures only — **no junior player
  names or any personal/guardian/medical data**.

Suggested walkthrough: **League/Arena** show the seeded setup; on **Game →
Coach** mark a selected skater *Can't play* → **Open Slot**; switch to
**Player**, pick an un-selected player and *Enroll as Substitute* → **Needs
Substitute Decision**; back on **Coach**, *Add* them → **Roster Confirmed**,
then *Lock Roster*. **Activity** shows the trail; **Public** shows the safe
fixture. *Reset* (top-right) restores the starting state.

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
