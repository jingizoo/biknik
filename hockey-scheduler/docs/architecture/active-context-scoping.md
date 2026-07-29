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
| `get_standings` | must match active | **required, must match exactly** | must match when one is selected |
| `get_setup_overview_v2` (Setup) | **the ceiling** | **hard ceiling** | narrows divisions, teams, clubs, officials |
| `list_players` (`/api/players`) | mandatory | — | narrows via `Team.league_id` |

## The #367 owner ruling, and what it reversed

Three of the rules above are the ruling's own corrections to what #369 shipped.
Each one reverses a decision that looked defensible in isolation, so the
reasoning is recorded here rather than rediscovered:

1. **Apply each axis through the strongest relationship that actually
   exists** — never a synthetic League foreign key bolted onto an entity that
   has none. Program is the hard ceiling for every scoped read;
   **Season-bound** rows must match the active Season (a Program-only context
   returns EMPTY for them, never the union); **League-bound** rows — Teams,
   Players, Clubs *through their Teams*, Divisions/registrations/standings,
   regular Games, and Officials *through an in-scope home Club or assignment* —
   must match the selected League.
2. **A league-less Exhibition** may appear under a selected League only when at
   least one participating Team validates into that League. "It has no
   `league_id`, so it is universally eligible" is the reasoning that put League
   B-vs-C exhibitions, both team names included, on League A's Dashboard.
3. **An unassigned record is not automatically "nobody's data."** Publishing
   installation-wide `unassigned_*` lists to every scoped operator was itself
   the disclosure — see [Unjoinable records](#unjoinable-records-and-the-pending-link-contract).

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

**A league-less Exhibition needs a participating Team in the selected League.**
An exhibition owns no `LeagueSeason`, and an early revision read that as
"universally eligible under every League", by analogy with a league-less Team.
The analogy fails: a league-less Team has no League to disagree with, while an
exhibition between two League B teams is concretely League B's fixture. Under
League A it surfaced both team names, the venue and the time. It is in scope only
when one of its participants is in the already-League-narrowed `teams` set — and
under "No League" (the Program + active-Season union) every exhibition in the
Season shows, as it should.

**Clubs, Organizations and Officials are scoped here too.** They carry no direct
Program foreign key, but no-foreign-key is not authorization to disclose — they
resolve through the same [derived joins](#derived-joins-and-the-pending-link-contract)
the Setup surface uses, and through the *strongest* axis each one actually has:

- **Clubs** are League-bound *through their Teams*, so they derive from the
  already Program+League-narrowed `teams` set.
- **Officials** are League-bound through an in-scope home Club, or through an
  assignment whose Game passes the complete active Season+League predicate —
  the same one the schedule uses, exhibition rule included.
- **Organizations** follow the facility axis instead (Season, never League),
  plus the active Program's own operating Organization.

Three successive revisions got this wrong: the first returned all three whenever
*no* Program resolved, the second kept returning them whenever one *did*, and the
third narrowed them to the Program but not the League — so with Program A /
Season A1 / League Aa active, sibling League A2's Club and Official still came
back, `home_club_name` and all. Unlike the Setup surface there is no bootstrap
counterpart on the Dashboard: this is an operational read with no create-flow
role, so a record linked to nothing is simply not Dashboard data.

**Fails closed with no exception.** When no Program resolves at all, every key
comes back empty. A scoped account with a missing, revoked or unassigned link
gets the same empty shape whether or not other Programs exist in the
installation. Pre-Program bootstrap reference data is served by the Setup
contract below instead.

**`setup_audit` is filtered by parent chain, against the whole tuple.** Each row
is resolved through its own `entity_type` (`season`, `division`, `team`,
`season_venue_access`, `game`, `official_assignment`, `calendar_feed_token` via
its actor, …) against the **same sets that decide whether the row's own entity is
returned at all** — `divisions`, `teams`, `in_scope_ls_ids`, `venues`, `rinks`,
the derived Club/Official sets, `_in_scope_game`. Reusing them is the point: a
parallel derivation drifted apart once already, leaving a Program-level-only
audit filter that re-widened every axis the rest of the method narrows, so
Season A2's divisions and League Ab's teams stayed in the feed by id and action
while the payload correctly withheld the entities themselves.

A row with **no** resolvable chain — `user_account`, `auth`, `guardian_link`,
`import_batch`, or any unrecognized future type — is **omitted** when scoping is
active, never guessed at and never shown globally. An unfiltered audit log leaks
actor ids and detail dicts across Programs.

### `get_standings`

The requested Division's validated `LeagueSeason → Season → Program` chain must
match the **active tuple**, not merely land inside the caller's authorization
ceiling.

**An active Season is required, and must match exactly.** Standings are
Season-bound by construction — a Division reaches its Season only through its
`LeagueSeason` — so a **Program-only context is indistinguishable from "that
Division does not exist"**, for every Division of that Program. An earlier
revision enforced the Season axis only `if season is not None`, which read "no
Season selected" as "every Season" and silently widened the endpoint exactly
where it should have closed: a caller whose context resolved to Program-only
(a saved Program-only selection, or a Program whose Seasons are all
unauthorized) could read any Division's standings in any Season of it.

The League axis *is* still conditional, and that asymmetry is deliberate rather
than an oversight: "No League" is a first-class **selection** meaning the
Program + active-Season union across every League, whereas "no Season" is the
absence of the ceiling this read is bound to.

Every mismatch — nonexistent, cross-Program, cross-Season, cross-League,
deleted, unbound, revoked, archived, unauthorized, Program-only — returns the
*identical* generic empty shape. The sameness is the security property:
distinguishing them would turn the endpoint into an existence oracle for records
the caller may not see.

### `get_setup_overview_v2` — Setup hub, six landings, Setup Records

The **active Program is the ceiling**, and `programs` collapses to just that one
Program. The context bar (`options_with_league`) is the cross-Program picker;
this structural surface only ever operates inside whichever Program is active.

**The active Season is a hard ceiling here too** — for the hub, all six workflow
landings and Setup Records alike. `seasons` is the active Season alone, and
`divisions`, the facility tree and Official assignments narrow with it; a
Program-only context returns **empty** for all of them. An earlier revision
resolved the Season and then deliberately ignored it, on the reasoning that a
management surface "needs to see and create against every Season in the
Program". The ruling rejects that: seeing another Season means **selecting** it,
exactly as switching Program or League already does.

Two axes are deliberately **not** narrowed here, and both are load-bearing:

- **`leagues`** is the active Program's full set. A League is *permanent Program
  structure* (#283), not Season-bound data, so neither the Season ceiling nor
  the League selection touches it — the pickers still need the Program's set,
  and the context bar remains the place a League is chosen. Its per-League
  `season_ids` binding list is likewise reported whole.
- **The facility tree** (Venue → Rink → IceSlot and the owning Organization)
  has a Season axis via `SeasonVenueAccess` and **no competition-League axis at
  all**, so a League selection never narrows it: under any League it stays
  active-Season-wide. "No League" is therefore exactly the **Program +
  active-Season union**. The UI says so in as many words (`setupScopeNote` in
  `web/static/app.js`, rendered on the hub, every landing and Records), because
  an operator who cannot see last season's division has to be able to tell a
  *selection* from a deletion.

A selected League narrows `divisions`, `teams`, and — per the ruling —
`clubs` (through their Teams) and `officials` (through an in-scope home Club or
assignment). "No League" means every League **in the active Program**, never
across Programs.

One client-side consequence worth knowing: the League create drawer's Season
picker cannot read `sv.seasons`, which now holds at most the active Season — it
reads the context bar's own authorized Season options instead
(`contextSeasonOptions`), and creating a Program or Season refreshes those
options immediately, so "create the Season, then its League" stays one
uninterrupted flow. That widens no read: those Season names are already
rendered in `#ctx-select` on the same page.

## Derived joins, and unjoinable records

`Club`, `Organization`, `Official`, `Venue`, `Rink` and `IceSlot` carry no
direct Program foreign key, but each has a real, validatable chain:

| Entity | In scope when… | Linked to *some* Program when… |
|--------|----------------|-------------------------------|
| Club | it has ≥1 Team in the active Program **and selected League** | it has ≥1 Team anywhere |
| Venue | it has a `SeasonVenueAccess` grant naming the **active Season**, **or** its legacy `league_id` is the active Program | it has **any** grant (active *or* revoked), **or** a non-null `league_id` |
| Rink / IceSlot | it cascades from an in-scope Venue | it cascades from a linked Venue |
| Organization | it owns ≥1 in-scope Venue, **or** it is the active Program's `operator_organization_id` | it owns a linked Venue, **or** it operates **any** Program |
| Official | its `home_club` is in scope, **or** it has an assignment whose Game sits in the **active Season** | its `home_club` is linked, **or** it has any resolvable assignment |

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
additive list. `Venue.league_id` is the third: despite the name it stores a
**Program** id (see [Venues have no League axis](#venues-have-no-league-axis)).

> **Subtlety worth keeping.** "Linked to no Program" for an Organization must be
> computed from *granted* Venues, not from owning any Venue at all. An
> Organization owning only ungranted Venues has no Program link and belongs in
> the unlinked set. Getting this wrong put such an Organization in **neither**
> list — invisible on the Setup facility tree, taking its (correctly-unlinked)
> Venues down with it, since a Venue renders under its owner and only a
> null-owner Venue reaches the orphan section.

### Unjoinable records, and the pending-link contract

An unjoinable record is **not** automatically "nobody's data". #369 shipped an
additive `unassigned_<entity>` list per kind, holding every record in the
installation linked to no Program and offered to **every** scoped caller, on the
reasoning that such a row belongs to no one yet and so discloses nothing. The
#367 owner ruling reverses that, and the reason is concrete: any Arena Manager —
including an identity authorized for **zero** Programs — could enumerate every
never-linked Club, Organization, Venue, Rink, IceSlot and Official in the
installation by name, and an Official's name is personal data.

Unjoinable rows are therefore **omitted** from scoped reads. What keeps the
create-then-link flows alive is the narrow, separately-authorized **creator-owned
contract** the ruling permits: `pending_link_<entity>` carries exactly the rows
that (a) have no validated link to any Program **and** (b) *this caller*
created, per the installation's own audit trail (`<entity>_created` with
`actor_id == user_id` — the write routes record the session account id, which is
the same id the read resolves its context from). The client unions each pair
through one helper (`withPendingLink(sv, key)` in `web/static/app.js`), so the
operator performing a create-then-link keeps seeing their fresh record right up
until its first real link narrows it — and nobody else ever sees it.

Two restrictions make that an ownership signal rather than a second disclosure
path, and both are asserted:

- **Only the CREATE entry counts.** Any later write (rename, reassign, delete)
  is ignored, so poking an id you were never shown can never become permission
  to enumerate it afterwards.
- **`user_id=None` owns nothing.** An identity-less caller gets empty lists
  rather than matching every `actor_id=None` row an internal or seed call path
  ever wrote.

One consequence to keep in mind when reading demo data: records created by the
seed (`actor_id="league_admin"`, not an account id) and never linked to
anything — the seeded club with no teams — are owned by nobody and therefore
show for nobody. That is the contract working, not a bug; linking such a record
(or recreating it through the UI) brings it back.

### Bootstrap vs. denial

These two look similar and must not be conflated:

- **Bootstrap** — the store has *zero* Programs anywhere. There is no "other
  Program" to leak from, so `get_setup_overview_v2` returns the full unfiltered
  shape, exactly as an unscoped (`role=None`) call does. This is what lets a
  brand-new install create its first records.
- **Denial** — the caller resolves no Program but other Programs *do* exist.
  Every derived-join set is naturally empty (nothing can validate against a
  Program that never resolved), so the scoped lists come back empty — and the
  `pending_link_*` lists hold only that caller's OWN unlinked creations, which
  is nothing at all for an identity that has created nothing.

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

**The guard has to cover module-level state, not just `render()`'s own locals.**
`ov` and `sv` are locals: a superseded render that bails simply paints nothing.
`playersList`, `hv`, `leagueTeams`, `seasonRegs` and `seasonVenueAccess` are
module-level and read at paint time, so a superseded render that assigned one of
them before bailing could leave the NEWEST render painting its own correctly
scoped cards next to another Program's player **names**. Every awaited fetch in
that block is followed by a generation check *before* its assignment. This was a
real, reproducible mixed-grid defect, not a theoretical one — it is what
`checkPlayerListRaceGuard` in `e2e/league-filtered-data.js` pins down.

## Coverage

- `tests/test_league_filtered_setup_progress.py`,
  `test_league_filtered_dashboard.py`, `test_league_filtered_standings.py`,
  `test_league_filtered_overview_v2.py` — each across Memory / SQLite /
  PostgreSQL and all seven roles, with the two-Program / two-League /
  two-Season matrix and the missing / deleted / unbound / revoked / archived /
  cross-Program / Program-only negatives. The last two files also carry
  authenticated-HTTP classes for the contracts that cross the route boundary:
  the Setup Season ceiling driven by the persisted context, the creator-owned
  pending-link list keyed on the session's own account id, and the
  Program-only standings read.
- `e2e/league-filtered-data.js`, `setup-v2-context-scope.js`,
  `dashboard-season-ceiling.js` — the browser layer at desktop and 390×844,
  including two delayed-response races proving the newest tuple always wins
  (one for `render()`'s locals, one for its module-level state).
