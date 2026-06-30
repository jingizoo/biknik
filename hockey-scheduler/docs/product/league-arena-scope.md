# League + Arena Setup — Slice Scope

The roster/substitute slice operates on a single seeded game. This slice adds
the **setup layer** an operator builds before games exist, so games can be
created against a real league/division and a real rink ice slot.

## Goal

> A league manager can create a league, season, and divisions; register clubs
> and teams; an arena manager can define venues, rinks, and ice slots; and a
> manual game can be created from two teams and one ice slot — ready to drive
> the existing roster/substitute workflow.

## In scope

- League → Season → Division hierarchy.
- Club → Team (a team belongs to a division).
- Venue → Rink → Ice Slot.
- Manual game creation referencing a season, division, two teams, and an ice slot.
- Validation: parent must exist; one game per ice slot; teams must match the
  division unless overridden; times must be timezone-aware UTC.
- Audit entry for every create.

## Out of scope (follow-ups)

- Automatic fixture generation / constraint solver
- Referee assignment and game-day operations
- Results, standings, playoffs
- Payments / ice billing
- Native SwiftUI screens and the public portal
- SQL persistence and auth/RBAC (still in-memory, single-tenant)

## Roles

| Role | Capability |
| --- | --- |
| League Director | Create league, season, division; create clubs/teams; create games |
| Arena Manager | Create venues, rinks, ice slots |
| (existing) Coach/Player/Guardian | Operate the roster/substitute workflow on a created game |
