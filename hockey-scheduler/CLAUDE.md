# CLAUDE.md — Hockey Scheduler

Guidance for working in the `hockey-scheduler/` project.

## Product

iPhone-first ice hockey league scheduling app. Build in **small,
domain-driven vertical slices**. The current slice is the game roster +
substitute workflow. Do **not** start the full season scheduler engine.

## Architecture principles

1. **Domain first.** The roster-status engine and the substitute state
   machine are pure functions/services over plain data. They have no I/O and
   no framework dependencies, so they are fully unit-testable. Keep business
   rules out of transport and storage layers.
2. **Layering.**
   - `domain/` — enums, dataclass models, errors. No logic that performs I/O.
   - `services/` — business rules: roster status calculation and the
     substitute workflow state machine. Emits domain events + audit entries.
   - `store/` — persistence behind a small repository interface. Currently an
     in-memory implementation; a SQL implementation can be swapped in later.
   - `api/` — a thin facade whose methods map 1:1 to the REST endpoints in
     `docs/architecture/api-contract.md`. Wire a web framework on top of this
     facade later without touching domain logic.
3. **Position awareness.** Goalie and skater slots are always counted
   separately. Never collapse them into one generic pool.
4. **Coach override always wins.** A coach/admin can manually add or remove a
   player, mark a slot intentionally open, and lock/unlock the roster.
   Overrides are allowed but must produce an audit entry.
5. **Everything is auditable.** Every state change appends an `AuditLog`
   entry via the service layer. Do not mutate state in a way that bypasses
   the audit trail.
6. **Notifications are events.** The service emits typed `NotificationEvent`s
   (see `docs/product/substitute-use-case.md`). Delivery (push/email) is a
   later concern; for now events are recorded and returned.

## Conventions

- Python 3.11, standard library only for this slice (no third-party deps so
  the suite runs anywhere). Use `dataclasses` and `enum`.
- IDs are opaque strings (`game_…`, `player_…`, `sub_…`).
- Times are timezone-aware `datetime` objects (UTC) passed in explicitly —
  the domain never calls `datetime.now()` itself, so logic stays
  deterministic and testable.
- Tests use `unittest`. Run: `python3 -m unittest discover -s tests`.

## Security

- No production secrets, no hardcoded live URLs, no real PII in seed/mock
  data. Seed data uses obviously-fictional names.

## Definition of done for a slice

- Acceptance criteria in the kickoff issue are covered by tests.
- Empty / loading / error states are represented in the API contract and the
  facade returns structured errors (not exceptions across the boundary).
- Docs in `docs/` describe the model, the API, and the use case.
