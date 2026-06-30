# MVP Scope — First Vertical Slice

## Goal

Implement the game roster + substitute workflow. This is the foundation for
game scheduling, team roster, attendance, substitute pool, coach approval,
notifications, audit trail, and (later) automated season scheduling.

## The vertical slice

```text
League Admin / Coach creates a game
  ↓ Coach selects game roster
  ↓ Players confirm availability
  ↓ Extra eligible players enroll as substitutes
  ↓ A selected player backs out
  ↓ System recalculates roster status (goalie & skater separately)
  ↓ If a substitute exists → "Needs Substitute Decision" (coach sees candidates)
  ↓ If no substitute exists → "Open Slot" (coach gets an action item)
  ↓ Coach offers the slot → substitute accepts → roster updates → slot closed
```

## Roles and permissions (first version)

| Role | Permissions |
| --- | --- |
| Coach / Team Manager | Select roster, view substitutes, offer & fill open slots, manual override, lock roster |
| Player | Confirm / back out; enroll as substitute if not selected; withdraw enrollment; accept/decline offers |
| Parent / Guardian | Respond on behalf of a junior player they are linked to |
| League Admin | View all games and roster status; same overrides as coach |

## In scope

- One game with a selected roster.
- Selected players confirm or mark unavailable.
- Non-selected eligible players enroll as substitutes.
- Recalculation of roster status when a player backs out.
- Position-aware open-slot counting (goalie vs skater).
- Coach offers / adds an enrolled substitute to the roster.
- Coach override (manual add/remove, mark slot open, lock roster).
- Audit trail for every state change.
- Notification events emitted for the substitute flow.

## Out of scope (explicitly)

- Full season scheduler / league scheduling engine
- Referee assignment
- Payments
- Stats / live scoring
- Chat / messaging delivery (events are defined, delivery is later)
- Automated substitute auto-fill (coach-approved filling only — see decision below)

## Opinionated decision: coach-approved filling

For the first version, substitutes are **offered by the coach**, not
auto-filled. Coaches care about position, level, discipline, fitness, and
fairness; junior teams may need guardian confirmation; auto-filling creates
disputes. Auto-fill (by position match → earliest enrolled → reliability →
coach priority → fairness rotation) can be added later behind a flag.

## Primary success behavior

> When a player backs out, the system immediately tells the coach whether
> there is a substitute available or whether the game has an open slot.
