# Active-context scoping — the read contracts (#345 / #367 / #369)

Part of epic #345 (persistent Program/Season/League context). #366 built the
context **bar**: an atomic, persisted, authorization-filtered selection of a
`(Program, Season, League)` tuple. That slice was display-only. This one makes
the reads that actually serve the Home, Dashboard, Standings and Setup surfaces
*respect* that tuple, so a client-side join over a globally-scoped payload stops
being the only thing standing between an operator and another Program's data.

## The tuple, and where it comes from

Every scoped read resolves the caller's context **server-side** through
`ContextService.resolve_with_league(user_id, role, scope)`
(`services/context_service.py`), which returns the exact detached
`(program, season, league)` objects it validated, any of which may be `None`.
No endpoint accepts a client-supplied Program/Season/League id for scoping — a
selection is a *view preference*, never authority, and is re-filtered through
the caller's real role + account scope (`services/context_scope.py`) on every
single request.

Two consequences worth stating plainly:

- **A selection can never widen authorization.** The authorized set is the
  ceiling; the active tuple narrows *within* it. Being globally authorized for
  every Program (League Admin, Arena Manager, Viewer — `context_scope.
  _GLOBAL_ROLES`) does **not** let you read a Program you are not currently
  active in. That was the #369 review's finding against `get_standings`.
- **An invalid saved selection is ignored, never rewritten.** If the saved
  League is deleted, unbound, cross-Program or newly unauthorized, it resolves
  to `None` (a first-class "no League" state) while the Program/Season stay
  intact — so restoring the record or the authorization restores the choice.

## Per-surface rules

These reads deliberately do **not** apply the same narrowing. The axis a
surface narrows on follows what that surface is *for*, and the differences are
the contract — not drift.

| Read | Program | Season | League |
|------|---------|--------|--------|
| `get_setup_progress` (Home / Tasks) | mandatory | resolved | narrows teams, roster, participation only |
| `get_demo_overview` (Dashboard) | mandatory | **hard ceiling** | narrows within the Season |
| `get_standings` | must match active | must match when one is active | must match when one is selected |
| `get_setup_overview_v2` (Setup) | **the ceiling** | not a further filter | narrows divisions + teams |
| `list_players` (`/api/players`) | mandatory | — | narrows via `Team.league_id` |

`list_players` is the one to remember when adding a Setup collection, because it
is easy to miss: it is not part of any overview DTO, it predates this work, and
it was still answering installation-wide long after the payload-shaped reads were
ceilinged. It returns player **names**, and on its MANAGE_SETUP route their
**emails** — which is exactly why the route already refuses to be folded into an
unauthenticated payload. A cross-Program answer there is the same disclosure by
another door. It narrows to Teams in the active Program whenever a role is
supplied, and further to the selected League; the no-context form stays
unfiltered for internal callers.

Two traps here, both of which shipped before being caught:

- **`team_id` is not authorization.** An early revision scoped only the
  *unfiltered* form (`if team_id is None and role is not None`), so
  `?team_id=<another Program's team>` skipped the ceiling entirely and returned
  that Team's players and emails. A caller-supplied id selects *which rows to
  consider*; it can never be evidence of entitlement to them. The ceiling
  applies to both forms.
- **Players DO have a League axis**, unlike Clubs/Organizations/Officials/
  Venues. They reach a League through their Team's real permanent
  `Team.league_id` (#283), so a selected League narrows them — and must, since
  `get_setup_progress`'s roster workflow and the Setup roster summary already
  do. Leaving only the Players card Program-wide contradicted its own screen.

### `get_setup_progress` — Home / Tasks hub

A selected League narrows the three *operational* workflows — "Permanent teams",
"Clubs, players and staff", and "Season participation" — via the real
competition `Team.league_id` and the Season's `LeagueSeason` bindings.

Two workflows are deliberately **invariant** to League selection:
`league_season` ("League profile and seasons") is a Program-wide structural
integrity check, and `facilities` has no competition-League axis at all (see
[Venues have no League axis](#venues-have-no-league-axis)).

### `get_demo_overview` — Dashboard

The resolved **Season is a hard ceiling**. An operator active in Season S1 never
sees S2's divisions, registrations, games, results, ice or standings snapshot —
even archived S2 history, even under the same League.

An earlier revision of this slice treated Season as *non*-restrictive, because
two existing journeys wanted cross-Season visibility. #369 review rejected that
as silently widening a released contract; the journeys were updated to switch
Season explicitly instead. **If a genuine all-Seasons inventory view is needed,
it must be its own explicitly named contract — not a quietly broadened
Dashboard.**

A Program-only active context (no Season resolved) narrows every Season-scoped
collection to **empty**, never falling back to "every Season" — a missing Season
selection must not silently re-widen the read.

**Clubs, Organizations and Officials are scoped here too.** They carry no direct
Program foreign key, but no-foreign-key is not authorization to disclose — they
resolve through the same [derived joins](#derived-joins-and-the-unassigned-buckets)
the Setup surface uses. Two successive revisions got this wrong in the same way:
the first returned all three whenever *no* Program resolved, the second kept
returning them whenever one *did*. Either way a scoped Coach could read another
Program's Club, Organization and Official names — and the official→club
association through `home_club_name`. Unlike the Setup surface there is no
`unassigned_*` counterpart on the Dashboard: this is an operational read with no
bootstrap role, so a record linked to nothing is simply not Dashboard data.

**Fails closed with no exception.** When no Program resolves at all, every key
comes back empty. A scoped account with a missing, revoked or unassigned link
gets the same empty shape whether or not other Programs exist in the
installation. Pre-Program bootstrap reference data is served by the Setup
contract below instead.

**`setup_audit` is filtered by parent chain.** Each row is resolved through its
own `entity_type` to a Program (`season`, `division`, `team`,
`season_venue_access`, `game`, `official_assignment`, `calendar_feed_token` via
its actor, …). A row with **no** resolvable chain — `user_account`, `auth`,
`guardian_link`, `import_batch`, or any unrecognized future type — is **omitted**
when scoping is active, never guessed at and never shown globally. An unfiltered
audit log leaks actor ids and detail dicts across Programs.

### `get_standings`

The requested Division's validated `LeagueSeason → Season → Program` chain must
match the **active tuple**, not merely land inside the caller's authorization
ceiling. Season and League are each enforced only when that axis is actually
selected, so a Program-only or Season-without-League context narrows less.

Every mismatch — nonexistent, cross-Program, cross-Season, cross-League,
deleted, unbound, revoked, archived, unauthorized — returns the *identical*
generic empty shape. The sameness is the security property: distinguishing them
would turn the endpoint into an existence oracle for records the caller may not
see.

### `get_setup_overview_v2` — Setup hub, six landings, Setup Records

The **active Program is the ceiling**, and `programs` collapses to just that one
Program. The context bar (`options_with_league`) is the cross-Program picker;
this structural surface only ever operates inside whichever Program is active.

Within it, `seasons` and `leagues` are the Program's *full* sets — Season is not
a further ceiling here, because a management surface needs to see and create
against every Season in the Program. A selected League narrows `divisions` and
`teams`; "No League" means every League **in the active Program**, never across
Programs.

## Derived joins, and the `unassigned_*` buckets

`Club`, `Organization`, `Official`, `Venue`, `Rink` and `IceSlot` carry no
direct Program foreign key, but each has a real, validatable chain:

| Entity | In scope when… | Linked to *some* Program when… |
|--------|----------------|-------------------------------|
| Club | it has ≥1 Team in the active Program | it has ≥1 Team anywhere |
| Venue | it has an **active** `SeasonVenueAccess` grant to one of the Program's Seasons, **or** its legacy `league_id` is the active Program | it has **any** grant (active *or* revoked), **or** a non-null `league_id` |
| Rink / IceSlot | it cascades from an in-scope Venue | it cascades from a linked Venue |
| Organization | it owns ≥1 in-scope Venue, **or** it is the active Program's `operator_organization_id` | it owns a linked Venue, **or** it operates **any** Program |
| Official | its `home_club` is in scope, **or** it has an assignment whose Game sits in one of the Program's Seasons | its `home_club` is linked, **or** it has any resolvable assignment |

The right-hand column is the one that is easy to get wrong, and getting it wrong
*is* the leak. A record with a chain to some **other** Program must be omitted;
a record with a chain to **no** Program is a different case entirely — a
just-created Club has no Team yet, and a just-created Venue has no grant yet, so
omitting those would deadlock the setup flow. The two columns therefore have to
be computed over the *same* edge set, or a record belonging to another Program
falls through the gap between them.

Two edges were missed on the first attempt and each leaked a name: a
**revoked** `SeasonVenueAccess` grant still ties a Venue to the Program that
revoked it, and `Program.operator_organization_id` ties an Organization to a
Program without any Venue being involved at all. Both had to be counted as
links, or the other Program's Venue and operating Organization surfaced in the
`unassigned_*` buckets. `Venue.league_id` is the third: despite the name it
stores a **Program** id (see [Venues have no League axis](#venues-have-no-league-axis)).

So each of the six also gets an additive `unassigned_<entity>` list holding
exactly the records linked to **no** Program at all. Offering those in any
Program's pickers discloses nothing about any other Program: an unassigned row
is nobody's data yet. The client unions each pair through one helper
(`withUnassigned(sv, key)` in `web/static/app.js`), so a fresh record stays
visible and selectable right up until its first real link narrows it.

> **Subtlety worth keeping.** "Linked to no Program" for an Organization must be
> computed from *granted* Venues, not from owning any Venue at all. An
> Organization owning only ungranted Venues has no Program link and belongs in
> `unassigned_organizations`. Getting this wrong put such an Organization in
> **neither** list — invisible on the Setup facility tree, taking its
> (correctly-unassigned) Venues down with it, since a Venue renders under its
> owner and only a null-owner Venue reaches the orphan section.

### Bootstrap vs. denial

These two look similar and must not be conflated:

- **Bootstrap** — the store has *zero* Programs anywhere. There is no "other
  Program" to leak from, so `get_setup_overview_v2` returns the full unfiltered
  shape, exactly as an unscoped (`role=None`) call does. This is what lets a
  brand-new install create its first records.
- **Denial** — the caller resolves no Program but other Programs *do* exist.
  Every derived-join set is naturally empty (nothing can validate against a
  Program that never resolved), so the scoped lists come back empty while the
  `unassigned_*` buckets still work.

## Venues have no League axis

`Venue.league_id` is **legacy vocabulary** and stores a *Program* id, not a
competition League id — confirmed by `store/integrity_checks.py`
(`JOIN seasons s ON s.program_id = v.league_id`). `get_setup_overview_v2` does
not expose it at all.

The only real join between the physical tree (Organization → Venue → Rink →
IceSlot) and the competition tree (Program → Season → League → Division) is
`SeasonVenueAccess`, which is deliberately a many-to-many: one Venue may host
several Seasons across different Programs and operators. **Never filter
venue-family data by matching a `league_id` field literally.**

Two more names in this family collide and are worth memorising:
`Team.league_id` *is* the real competition League (#283), while
`get_demo_overview`'s `team_rows[].league_id` JSON key is populated from
`team.program_id` and is legacy-vocabulary "Program", retained for backward
compatibility.

## Client-side staleness guards

Because these reads now depend on the active tuple, a context switch landing
mid-flight can otherwise paint a newer context's screen with an older context's
data. `render()` snapshots `contextRevision` before its awaited fetches and
discards the response if the context changed — the same monotonic-generation
idiom already used by `setupProgressFetchSeq`, `drawerSeedFetchSeq` and
`iceOperationSeq`.

## Coverage

- `tests/test_league_filtered_setup_progress.py`,
  `test_league_filtered_dashboard.py`, `test_league_filtered_standings.py`,
  `test_league_filtered_overview_v2.py` — each across Memory / SQLite /
  PostgreSQL and all seven roles, with the two-Program / two-League matrix and
  the missing / deleted / unbound / revoked / archived / cross-Program
  negatives.
- `e2e/league-filtered-data.js`, `setup-v2-context-scope.js`,
  `dashboard-season-ceiling.js` — the browser layer at desktop and 390×844,
  including a delayed-response race proving the newest tuple always wins.
