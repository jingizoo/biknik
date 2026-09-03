# Substitute Use Case

## Three groups of players per game

| Group | Meaning |
| --- | --- |
| Selected roster | Players originally picked for the game |
| Confirmed players | Selected players who confirmed they can attend |
| Substitute pool | Eligible players not selected but willing to play if a slot opens |

## Key rule

When a selected player backs out:

```text
recalculate roster status (goalie and skater counted separately)
If an open slot exists AND the substitute pool has eligible enrolled players
    game status = Needs Substitute Decision   (coach sees candidates)
Else if an open slot exists
    game status = Open Slot                    (notify coach / team manager)
Else
    game status = Roster Confirmed / Awaiting Responses
```

## Substitute state machine

```text
Not Selected
  → Enrolled (in substitute pool)
      → Offered (coach offers the slot)
          → Accepted  → added to game roster, slot closed
          → Declined  → back to pool / removed
          → Expired   → terminal history; active enrollment released
      → Withdrawn (player leaves the pool)
```

Allowed transitions:

| From | To | Trigger |
| --- | --- | --- |
| (none) | enrolled | player enrolls |
| enrolled | offered | coach offers slot |
| enrolled | withdrawn | player withdraws |
| offered | accepted | substitute accepts |
| offered | declined | substitute declines |
| offered | expired | offer window passes |
| offered | enrolled | coach rescinds offer (back to pool) |

## Bounded cross-team availability (#287)

The September 4, 2026 slice adds the narrow player journey requested for
same-Division borrowing; it does not add the full ranking engine or
cross-Division policy:

1. A signed-in player opens **Home** and sees proactive **I can substitute**
   checkboxes for upcoming published Games before any vacancy exists.
2. Each checkbox names one explicit target team. A Game can show two choices,
   one for each participating side; the Game id alone never chooses a side.
3. The player's active source membership and the target team's registration
   must resolve through the exact same `LeagueSeason` and the same non-null
   `Division`. The source team must be neither side in that Game. Exhibition,
   cross-LeagueSeason, cross-Division, own-game, locked, cancelled, draft and
   already-started choices are not offered.
4. Checking a box creates one active enrollment owned by that target side.
   `team_id` is the durable target; the exact source membership and source team
   are stored privately as provenance. At most one active enrollment exists
   for that player and Game, so checking one side excludes the other even under
   concurrent requests.
5. An enrolled row does not promise a seat. The target coach can offer or seat
   it only when that target has an open slot matching the source membership's
   snapshotted goalie/skater type, and the exact source provenance still
   validates.
6. A cross-team offer gets a server-owned deadline of
   `min(offered_at + 30 minutes, game.start_time)`. The response window is
   half-open: accept or decline must commit before the deadline; at equality
   `EXPIRED` wins. The transition records `substitute_expired` audit evidence.
   The stale card presents **Dismiss Expired Offer**, which uses the decline
   response path to persist expiry and release the unique active lifecycle.
7. A verified guardian may accept or decline an offer for a junior. Proactive
   cross-team opt-in and withdrawal are player-only in this slice.

The established same-team workflow is unchanged: it omits `target_team_id`,
uses its existing live side resolution and deadline semantics, and does not
populate cross-team source provenance.

Every player/guardian cross-team detail, withdrawal, acceptance or decline
carries the target team shown to them. That target is checked against the
active row inside the transaction; a stale tab for an earlier target gets 409
and cannot alter the newer enrollment. Same-team requests keep omitting the
target; the scoped Player Home routes use `{}`, while the documented generic
alias may still carry its ignored compatibility `actor_id` field.

## Game roster status state machine

```text
Draft → Selected → Awaiting Responses → Roster Confirmed
                                   ↘ Needs Substitute ↔ Open Slot
Any non-final state → Locked (coach) → Final
```

| Status | Meaning |
| --- | --- |
| Draft | Game created, roster not selected |
| Selected | Coach selected players, awaiting responses |
| Awaiting Responses | Some selected players have not responded |
| Roster Confirmed | All target slots filled by confirmed players |
| Needs Substitute | An open slot exists and eligible substitutes are enrolled |
| Open Slot | An open slot exists and no eligible substitutes are enrolled |
| Locked | Coach locked the roster; no player changes without unlock |
| Final | Game played / archived |

## Notification events

| Event | Sent to | Message |
| --- | --- | --- |
| `player_backed_out` | Coach | "A selected player is unavailable." |
| `slot_open` | Coach | "1 skater slot open. No substitutes enrolled." |
| `substitute_enrolled` | Coach | "A player enrolled as substitute." |
| `substitute_offered` | Player / parent | "A game slot is available. Accept?" |
| `substitute_accepted` | Coach | "Substitute accepted and was added to roster." |
| `roster_locked` | Team | "Roster is locked for this game." |

## Edge cases and required behavior

| Edge case | Behavior |
| --- | --- |
| Two substitutes accept the same slot | First accepted wins; second sees slot already filled (error) |
| Player withdraws after being offered | Legacy same-team behavior remains: offer withdrawn and coach notified. A live cross-team offer requires accept/decline; at or after its deadline the response records `EXPIRED`. |
| Goalie backs out | Only goalie-eligible substitutes count toward "Needs Substitute" for the goalie slot |
| Junior player response | Linked guardian may respond on the player's behalf |
| Coach manually adds player | Override allowed; audit entry required |
| Player already selected | Cannot also enroll as a substitute |
| Player from another team | Eligible only for an explicit target whose active registration shares the source membership's exact `LeagueSeason` and same non-null `Division`; the source team cannot be a Game side |
| Roster locked | No player changes until coach/admin unlocks |
| Game cancelled | Substitute enrollments cancelled |
| No substitutes enrolled | Show open slot clearly; notify coach |
