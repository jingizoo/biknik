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
| iOS app (SwiftUI) | ⛔ out of scope for this slice — screens are specified in `docs/` |
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
      seed.py     mock data builder
    tests/        unittest suite (no external deps)
```

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
