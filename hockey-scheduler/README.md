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
      web/        iPhone-framed demo: stdlib HTTP server + static UI
      seed.py     mock data builder
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

It has two views via a segmented control:

- **Coach** — Game Detail with the roster-status banner, selected players (mark
  *Can't play*), the substitute pool (*Add* a sub), and *Lock Roster*.
- **Player** — pick any player to see their personalized screen: confirm / back
  out if selected, *Enroll as Substitute* if not, or *Accept/Withdraw* an offer.

Suggested walkthrough: in **Coach** view mark a selected skater *Can't play* →
the banner flips to **Open Slot**; switch to **Player**, pick an un-selected
player and *Enroll as Substitute* → the coach banner becomes **Needs Substitute
Decision**; back in **Coach**, *Add* that substitute → the slot closes and the
roster is **Confirmed** again. *Reset* (top-right) restores the starting state.

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
