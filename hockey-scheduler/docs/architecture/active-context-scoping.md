# Active-context scoping — the data contracts (#345 / #367 / #369)

Part of epic #345 (persistent Program/Season/League context). #366 built the
context **bar**: an atomic, persisted, authorization-filtered selection of a
`(Program, Season, League)` tuple. That slice was display-only. This one makes
the reads that actually serve the Home, Dashboard, Standings and Setup surfaces
*respect* that tuple, so a client-side join over a globally-scoped payload stops
being the only thing standing between an operator and another Program's data.

Mostly reads — but not only. The #369 review established that scoping a read
without gating the matching **write** leaves the boundary open from the other
side: a caller who cannot *see* another creator's Venue could still POST a Rink
under it. See “Scoping the read is not enough: the parent-id WRITE gate”.

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
| `get_setup_progress` (Home / Tasks) | mandatory | **hard ceiling** for participation/facilities (Program-only is empty); league_season/teams/roster unaffected | narrows teams, roster, participation only |
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

**The active Season is a hard ceiling for the two Season-BOUND workflows** —
"Season participation and divisions" (a SeasonTeamRegistration/Division
reaches its Season only through a `LeagueSeason`) and "Venues, rinks and ice"
(reaches its Season only through a `SeasonVenueAccess` grant) — per the #367
owner ruling: **Program-only must be empty for those workflows**, never the
union of every Season the Program has. `in_scope_season_ids = {season.id} if
season is not None else set()` is the exact idiom `get_demo_overview` already
uses for its own Season-scoped collections (see below); this read names the
same set for the same reason, so both contracts stay auditable against one
pattern instead of two independently-invented ones.

The other three workflows are deliberately **not** Season-bound, and stay
identical across every Season selection, Program-only included:
`league_season` is a Program-wide integrity check *about* every Season
collectively (not a per-Season fact — "does EVERY Season have a League"),
and `teams`/`roster` ("Permanent teams" / "Clubs, players and staff") key off
`Team`/`Player`, neither of which carries a Season field at all — a selected
League can narrow them (see above), but no Season selection ever does.

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
installation. Pre-Program reference data is served by the Setup contract's
creator-owned `pending_link_*` lists below instead — and that is the *only*
route to it, on a brand-new install as much as on an established one.

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

Four restrictions make that an ownership signal rather than a second disclosure
path. Each is asserted in `tests/test_pending_link_ownership.py` across
Memory/SQLite/PostgreSQL **and** authenticated HTTP, and each was
mutation-proven — reverting the guard makes the matching assertion fail:

- **Ownership is the authenticated account id, never a caller-supplied value.**
  `actor_id` on the create audit row is always the server-resolved session
  `user_id` (`_resolve_role` reads it from the server-side session record), and
  `user_id` on the read is the same value. No write path lets a request body
  influence it: `_handle_setup` / `_handle_setup_v2` take `actor_id` as a
  parameter the router supplies, and `test_actor_attribution.py` rejects a
  forged body `actor_id` outright.
- **Only the CREATE entry counts.** Any later write (rename, reassign, delete)
  is ignored, and `action` must equal `f"{entity_type}_created"` — both halves,
  so a `club_created` row filed under `entity_type="venue"` matches nothing.
  Poking an id you were never shown can never become permission to enumerate
  it afterwards.
- **`user_id=None` owns nothing.** An identity-less caller gets empty lists
  rather than matching every `actor_id=None` row an internal or seed call path
  ever wrote.
- **A pending row's parent LABELS resolve only against what the caller can
  already see** — its own scoped list plus its own pending list. Ownership
  authorizes *the row*, never the foreign records it points at. Without this,
  the create routes were a name oracle: they take a parent id verbatim from the
  request body and never check the caller may see that parent, and ids are
  sequential, so an Arena Manager (MANAGE_ARENA covers the venue/rink/ice-slot/
  organization creates *and* this read, at zero authorized Programs) could POST
  a Rink at `venue_N`, an IceSlot at `rink_N`, a Venue at `org_N` or an Official
  homed at `club_N` and read another operator's never-linked `venue_name` /
  `rink_name` / `organization_name` / `home_club_name` straight back out of its
  own pending row. The parent *id* the caller itself supplied stays; the name
  goes `null`. The caller's own create-then-link chain is unaffected — a Rink
  under a Venue it created still renders that Venue's name.

**Lifecycle.** A row leaves `pending_link_*` the instant any validated Program
link exists, and reappears in the normal scoped list for the Program it is now
linked to (a Club once a Team is in it; a Venue once a `SeasonVenueAccess`
grant exists, with its Rinks/IceSlots cascading and its owning Organization
following through the Venue). It is then that Program's data, governed by the
active-tuple ceiling like everything else — so a second operator authorized for
that same Program *does* get it, through the scoped list, and never through
anyone's pending list.

**A deactivated (or departed) creator's rows go to nobody.** They stay keyed to
that account's id, which no other session can present — deactivation revokes
the account's sessions *and* `SessionManager.resolve` independently rejects a
session whose account is inactive, and login refuses it. So the rows fail
closed: not inherited by the admin who deactivated the account, not released
into any installation-wide pool, and not picked up by a later account (ids come
from a monotonic per-prefix counter and are never recycled, so even reusing the
freed username yields a different id). Reactivation restores exactly the
creator's own view. There is deliberately **no** "creator account must still
exist" check inside the read: `user_id` *is* the caller's proven identity,
established at the HTTP boundary, and re-deriving authorization from account
state would add a second, weaker signal that an attacker able to present the
id has already bypassed anyway.

**Demo seed.** Seeded records are attributed to `full_demo.DEMO_ADMIN_ACCOUNT_ID`
(`"user_admin"`) — the account id `_seed_demo_accounts` / `_seed_admin_account`
actually mint for the "admin" persona. This used to be the string
`"league_admin"`, which is a *role name*, not an account id: no session's
`user_id` could ever equal it, so every seeded record with no Program link was
owned by nobody and visible to nobody — concretely the 17 seeded Officials with
neither a home Club that has a Team nor a game assignment, present in the store
and unreachable in the UI. The ruling's remedy is to link such data or attribute
it to a real account; a read-side bypass for synthetic actor labels is **not**
an option, since it would hand rows to callers who authenticated as nobody and
reopen the disclosure the ruling reversed. The test resolves the seed's actor
against the seeded accounts, so a rename on either side fails there rather than
silently orphaning the demo data again.

### Bootstrap is not an exception (reversed)

An earlier revision drew a line here between "bootstrap" and "denial" and gave
the first one a bypass: when the store held **zero Programs anywhere**, a
scoped caller fell through to the full unfiltered shape, exactly as an
unscoped (`role=None`) call does, on the reasoning that such an install has no
"other Program" to leak from. **That carve-out is reversed.** It is neither the
owner-approved creator-owned contract nor a separately authorized install-admin
boundary, and the reasoning does not hold: a Program-less installation is
precisely where *pre-Program* rows accumulate — Clubs, Organizations, Venues,
Rinks, IceSlots and Officials, the last of which are people's names — so two
setup-capable accounts on the same fresh install could each enumerate
everything the other had entered, and an account authorized for nothing could
enumerate all of it. **Absence of a Program is not an authorization.**

There is therefore **one rule, not two**. An authenticated caller that resolves
no active Program gets the same answer whether or not any Program exists
anywhere:

- every derived-join set is naturally empty (nothing can validate against a
  Program that never resolved), so the scoped lists come back empty;
- the `pending_link_*` lists hold only that caller's **own** unlinked
  creations — nothing at all for an identity that has created nothing.

Collapsing the two cases into one code path is itself the safeguard: there is
no "but the install is empty" branch left for a future change to widen.

**This does not deadlock a new installation.** The operator bootstrapping a
brand-new install *is* the account that created the rows it is working on, so
they come back through `pending_link_*` and `withPendingLink` unions them into
the Setup Records cards and every create drawer picker — create-then-link is
unchanged. The moment that operator creates the first Program,
`ContextService._fallback` selects it (it is the only authorized Program, no
explicit selection needed) and the normal scoped chains take over. What
changes is only that a *second* operator's pre-Program rows stay that
operator's.

The one contract deliberately left alone is the identity-less legacy call:
`get_setup_overview_v2()` with `role=None` is still fully unfiltered, because
several internal call sites read the whole installation view through it. The
blocker — and this rule — is about **authenticated** callers.

Regression: `backend/tests/test_zero_program_bootstrap_scoping.py` (facade
across Memory/SQLite/optional PostgreSQL, plus authenticated HTTP on a
cold-boot `STATE.reset(seed=False)` install).

### Scoping the read is not enough: the parent-id WRITE gate

Four setup creates take a parent id straight from the request body — Rink→Venue,
IceSlot→Rink, Venue→Organization, Official→home Club. Scoping the *read* and
redacting the resolved parent **name** out of `pending_link_*` closed the
read-side oracle and left the write itself wide open. Two disclosures survive
name redaction:

- **success vs. not-found is itself an existence oracle** over sequential ids
  (`venue_9` accepted, `venue_99` refused); and
- **the write really lands.** Another creator's private setup graph silently
  grows a Rink, its Rink grows ice, its Organization grows a Venue, its Club
  grows an Official carrying a person's name.

So the parent id is validated before the facade call, on **both** API versions
(`/api/setup/` is not a bypass) and on **both verbs** (create and reassign — see
below), by `Handler._reject_parent_outside_scope` →
`ApiService.writable_setup_parent_ids`. The accepted set is computed from the
read itself, so the write gate cannot drift into a weaker policy, and has
**three** sources:

| source | why |
| --- | --- |
| the ACTIVE scoped list | the ordinary case — the active-tuple ceiling as the read applies it |
| this caller's `pending_link_*` | its create-then-link chain: a Rink under a Venue it just made, before any grant exists |
| rows this caller **created** | see below — required, not a convenience |

The third source is not optional. *Create an Organization → create a Program it
operates → add that Organization's first Venue* is an ordinary flow, and at the
moment of the Venue create the Organization is in **neither** of the first two
lists: operating a Program is a real Program link (so it is correctly not
pending), while the caller's context still points at another Program (so it is
correctly not scoped). It falls between them, though the caller made it moments
ago. Omitting this source cost 20 backend failures.

Creator-ownership is the same authenticated, unforgeable signal the pending-link
contract already rests on (`_creator_created_ids`: CREATE actions only, real
account ids only, so probing an id can never become permission to use it). It is
**strictly narrower than authorization** — a `_GLOBAL_ROLE` like League Admin is
authorized for every Program yet has still created only its own rows — which is
exactly why this is not the authorized-set alternative the ruling rejected.

Refusals reuse the facade's own `"<Label> <id> not found."` wording, so an
inaccessible parent is byte-identical to one that never existed, and only the
caller's own input is echoed back. The gate fails **closed**: an unresolvable
identity or an errored overview refuses rather than falling through to the
write. A falsy parent id is not this gate's business — that stays the facade's
own validation error.

**Creates are not the only verb.** `assign-<target>` *moves* an existing record
under a new parent, and a gate on creates alone leaves the identical write open
behind a different URL — proven, before it was closed, by an Arena Manager
moving its own Rink under another creator's Venue and getting a **200 on both
API versions**. The same gate therefore runs on both reassign handlers, driven
by `_REASSIGN_PARENTS`:

| relation | body key | API |
| --- | --- | --- |
| `rink` → `venue` | `venue_id` | v1 + v2 |
| `venue` → `organization` | `organization_id` | v1 + v2 |
| `team` → `club` | `club_id` | v1 + v2 |
| `league` → `organization` | `organization_id` | v1 (legacy: v1 “league” **is** today’s Program) |
| `program` → `organization` | `operator_organization_id` | v2 |

An explicit **null** id — the unassign on the nullable relations — is deliberately
not gated: there is no parent to leak and nothing to probe.
`division`→`league`, `team`→`league` and `player`→`team` have different parent
kinds and are governed elsewhere.

Regression: `backend/tests/test_setup_parent_write_scope.py` (v1 parity, the
global-role case, the operator-org flow from both sides, the reassign verb on
both APIs, and a table-completeness check so a relation dropped from
`_REASSIGN_PARENTS` cannot reopen a write silently) plus
`test_http_writing_under_a_guessed_parent_id_is_refused_outright` in
`backend/tests/test_pending_link_ownership.py` — which is the **reversal** of an
assertion that previously required only that the probe creates come back with
the name withheld.

### Named schedule scenarios (#378 / #381)

The same blocker, one surface later. The four scenario routes shipped gated on
the `MANAGE_SCHEDULE` **capability** alone and on nothing else: `POST
/api/scheduler/scenarios` took the Program/Season/League/Division ids straight
from the request body, `GET /api/scheduler/scenarios` returned every stored
scenario in the installation, and get/commit-by-id reached any of them.

League Admin **and Arena Manager both hold `MANAGE_SCHEDULE`**, and both are
`context_scope._GLOBAL_ROLES` — authorized for every Program. So an operator
whose active tuple was Program B could create a Program A scenario, read A's
name, creator, constraints, whole proposal and generation snapshot, and commit
A's frozen Games into A. A cross-context disclosure and an IDOR write behind the
same missing check. *One asserted capability fact is not the whole workflow
capability* — the #369 rule, restated.

The fix reuses the machinery already here rather than inventing a second one:

| verb | bound how |
| --- | --- |
| create | the requested ids are resolved by `resolve_scenario_scope` into a real `(Program, Season, League)`, and **that** whole edge must equal the active tuple — checked before the planner runs, so no proposal is ever computed for a foreign hierarchy |
| list | filtered on the STORED ROWS, strictly **before** any DTO is built |
| get | one raise site shared with "does not exist" |
| commit | **re-authorized at commit time**, under the scenario's row lock, inside the transaction that writes the Games |

A scenario's `(program_id, season_id, league_id)` is judged by
`_setup_target_edge_allows` **verbatim** — the same "edges, not unions"
predicate #369 landed for setup targets. All three columns are NOT NULL, so:

- a **Program-only** context fails closed against every scenario, exactly as
  `get_standings` does — a scenario is Season-bound by construction and there is
  nothing to compare a missing Season against;
- an explicit **No League** stays the first-class "approved Program +
  active-Season union" selection it is everywhere else, and the Season ceiling
  above it does not relax;
- the two near misses the owner named — same Program/different Season, and same
  Season/different League — are both refused, which is exactly what a
  Program-only ceiling would have waved through.

**There is no creator clause.** `setup_target_accessible` rule 6 admits one only
for a genuinely UNLINKED record, and a scenario can never be unlinked. Creator
authority surviving hierarchy linking was ruled a blocker in its own right in
#372 (`writable_setup_parent_ids` was too broad): it is an unrevokable back door
no Program admin can see or remove. Once the chain exists, the chain is the sole
authority.

**Filtering the list before DTO construction is the contract, not an
optimization.** Building DTOs and then dropping some would mean a foreign
scenario's name, creator, constraints, whole proposal and generation snapshot
had already been deep-copied into a response object once; the count, the shape
and the timing of that work are a signal on their own, and it is one `return`
away from being the payload.

**Foreign and nonexistent are response-identical, in status AND bytes.** Get and
commit share ONE raise site, so `404 not_found` /
`{"reason": "schedule_scenario_missing", "scenario_id": <the caller's own
input>}` is all either produces; mask that one echo and the payloads are equal.
Create's refusal reuses the wording `resolve_scenario_scope` already emits for
the same request SHAPE — `division_missing` for the Division form,
`league_season_missing` for the Season+League form — so a foreign hierarchy is
indistinguishable from a guessed one. A 404-vs-403 split here would itself be
the disclosure: it turns a sequential id space into an existence oracle.

**Commit re-authorizes; it does not trust the create.** Reading a scenario at
generation is not authority to commit it minutes later — the operator may switch
Program, Season or League in between, and the tuple that decides is the one
current when the Games land. So the check is not a preflight. It runs after
`get_schedule_scenario_for_update` has row-locked the scenario, on the post-lock
snapshot, inside the SAME `store.transaction()` the draft-commit gate joins for
every Game INSERT, slot allocation, counter bump and audit row — the
lock-then-decide-then-mutate shape `setup_guarded_mutation` established, for the
reason #372 gives: a predicate that closes its own transaction before the write
was never one unit with it. An identified caller opens that transaction
`SERIALIZABLE`, because `ContextService._snapshot` asks for it and a nested join
may not RAISE the open transaction's isolation.

`role is None` — internal call sites, the demo/full seeds, the acceptance
harnesses — is ungated and completely untouched, matching
`setup_target_accessible` rule 1.

Regression: `backend/tests/test_schedule_scenario_scope.py`, across
Memory/SQLite/PostgreSQL at the service boundary and over authenticated HTTP.
Two Programs, with Program A carrying both near-miss corners, each corner
independently schedulable so a refusal can never be confused with an empty one.
Every negative is measured against the clause-5 control — the same actor, in the
scenario's exact tuple, still creating, reading, listing and committing — and
the HTTP matrix asserts the control again on the far side of the refusal, so
switching back restores exactly the authority switching away removed.

Mutation-proven, one falsifying mutation per independent clause; see the PR
description for the verbatim failures.

## The grant-only facility contract (venue sharing)

The ceiling governs the **competition** tree — Seasons, Leagues, Divisions,
Teams, and the personal names on Players and Officials. The facility tree
(Organization → Venue → Rink → IceSlot) is joined to it only by
`SeasonVenueAccess`, and there the ceiling alone produced a **deadlock**:

> An arena serves several leagues. Once Program A grants itself access to one,
> that Venue is linked to A and therefore leaves Program B's scoped `venues` —
> and it is not `pending_link_venues` either, because it *is* linked. So B can
> never grant itself the access that would have made it visible. The capability
> fails on its own first use.

**The first fix for this was wrong, and the reason is the point of this
section.** It added a `grantable_venues` field to `get_setup_overview_v2`.
But `/api/v2/setup/overview` is gated `MANAGE_ARENA` while the grant POST needs
`MANAGE_SETUP` — so an Arena Manager, a role that *cannot perform the sharing
action at all*, received every linked Venue's id and name regardless of its
active Program. The disclosure was not bounded to the feature that needed it,
and ordinary read visibility had quietly become write authorization.

So the candidate list lives on its own route:

`GET /api/v2/setup/seasons/<season_id>/venue-candidates` → `MANAGE_SETUP`

bounded four ways:

| bound | why |
| --- | --- |
| its own `MANAGE_SETUP` route | the same permission as the grant it feeds, so no role learns a Venue it could not already act on |
| the destination Season must **be** the persisted **selected** Season | a sibling Season of the active Program, a foreign one and a nonexistent one are all refused identically, so this is neither a widened grant surface nor a Season oracle |
| id + name only | a physical building's existence, not anybody's data |
| **linked** venues, or the caller's **own** unlinked draft | another operator's never-linked Venue is a private draft governed by the creator-only `pending_link_venues` contract. An earlier revision returned every Venue and leaked one operator's arena to another on a Program-less install. |

Already-granted Venues are deliberately **not** candidates, so
`list_season_venue_access` names its own Venue (`venue_name`, additive): a
cross-Program Venue this Season already uses has nowhere else to resolve its
name from, and the Allowed-venues row would otherwise render a bare id.

**That addition needed the same ceiling, and at first did not have it.** The
justification above ends "…to a caller already reading that Season's grants" —
but `GET /api/v2/setup/seasons/<id>/venue-access` had only a role-level
`MANAGE_SETUP` gate and took the requested Season id on trust, so *which*
Season's grants a caller was reading had never been established. With Program A
active, asking for Program B's Season answered `200` with B's Venue id **and**
its name, while a guessed Season id answered `200 {"venue_access": []}` — one
route, a cross-Program facility disclosure and a Season-existence oracle.
Role-level permission says the caller may manage *some* Season's grants; only
the persisted active tuple says *which*.

So the read is bound exactly like the candidate route beside it: identity goes
through to the service, the requested Season is checked against the persisted
active tuple, and every miss takes one generic `not_found` path. `venue_name` is
serialized only past that check.

### The exact selected-Season ceiling (owner ruling)

That check was first written as a **Program** comparison
(`season.program_id == active_program.id`), for a client reason: the Setup tree
shows every Season of the active Program, and `app.js` read allowed venues for
each one. The repository owner ruled that insufficient —

> Use the exact selected-Season ceiling. The same-Program/other-Season 200
> remains incorrect. Both the venue-access list and candidate route must require
> the requested Season to equal the persisted selected Season; Records must stop
> using the endpoint as an all-Seasons inventory.

So **both** routes now require `season_id == resolved_active_season.id`:

| requested Season | answer |
| --- | --- |
| the persisted **selected** Season | `200`, unchanged in every respect |
| a **sibling** Season of the active Program | `404`, generic |
| a Season of another Program | `404`, generic |
| a Season that does not exist | `404`, generic |
| any Season, with a **Program-only** context | `404`, generic — there is no selected Season for the request to equal, so it fails closed |

All four refusals are byte-identical once the echoed id is masked.

The ceiling is the **destination Season**, never the candidate *set*: a Venue
linked only to another Program is still a candidate (that exception is the whole
reason this contract exists — without it arena sharing deadlocks on its own
first use), and a shared cross-Program arena the selected Season already holds is
still named by `venue_name`.

The all-Seasons client walk that motivated the Program ceiling is gone.
`app.js`'s Setup render loop fetches both routes **only** for the selected
Season; every other Season in the hierarchy renders "Select this season to
manage its venues" in place of its allowed-venues list and its Allow picker —
no venue ids, no venue names, no count, and no request issued. Deliberately no
new batch endpoint: that would re-create the inventory the ruling removed, one
HTTP hop further down.

`get_setup_overview_v2` is unchanged and fully ceilinged — it carries no
candidate list at all.

Regression: `backend/tests/test_facility_tree_exception.py` — the ordinary
overview enumerating no foreign Venue for League Admin, Arena Manager and a
zero-scope identity with Program B active; the candidate set across active,
revoked-only and legacy links plus another creator's draft; identical refusal
for foreign and nonexistent Seasons; the `MANAGE_SETUP` boundary at the route;
and zero grant/audit mutation on a refused read. For the grant READ the same
file pins, on Memory/SQLite/PostgreSQL **and** authenticated HTTP, that a
foreign and a nonexistent Season produce byte-identical bodies once the echoed
id is masked, naming nothing of the other Program's, while the active Program's
own Season still returns its active *and* revoked rows with `venue_name` —
including a legitimately shared cross-Program arena.

`SelectedSeasonCeilingTest` / `SelectedSeasonCeilingHttpTest` in the same file
pin the owner ruling for **both** routes on Memory/SQLite/PostgreSQL and over
authenticated HTTP: the selected Season still returns its rows and its
candidates (including the cross-Program shared arena and a cross-Program
candidate); a **sibling** Season of the active Program, a foreign one and a
nonexistent one all refuse with identical raw bytes once the echoed id is
masked; a Program-only context fails closed; and grants plus setup-audit are
unchanged after every refusal. Each fixture asserts its own preconditions first
(the sibling Season really shares the selected Season's Program, the shared
arena really is held by both, the Venue names really are distinct), or the
refusal assertions would pass with the fix reverted.

Mutation-proven: gating the candidate route `MANAGE_ARENA` fails with *"an
Arena Manager reached the grant-candidate contract"*; dropping either route's
target-Season check fails the oracle assertions on every backend (*"Program B's
Season was readable while Program A was active"*); and relaxing either route
back to the Program comparison fails on Memory, SQLite, PostgreSQL and HTTP
with *"the sibling Season was readable while another Season was selected"* and
*"a Season of the active Program was readable with NO Season selected — there
is no selected Season for the request to equal"*.

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
- `tests/test_schedule_scenario_scope.py` — the named-scenario create / list /
  get / commit boundary (#381) across Memory / SQLite / PostgreSQL and over
  authenticated HTTP, with the two-Program matrix, both near-miss corners, the
  foreign-vs-nonexistent byte comparison, the switch-between-create-and-commit
  case, and the positive control every negative is measured against.
- `e2e/league-filtered-data.js`, `setup-v2-context-scope.js`,
  `dashboard-season-ceiling.js` — the browser layer at desktop and 390×844,
  including two delayed-response races proving the newest tuple always wins
  (one for `render()`'s locals, one for its module-level state).
