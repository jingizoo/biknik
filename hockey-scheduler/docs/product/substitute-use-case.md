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
          → Expired   → back to pool / removed
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
| Player withdraws after being offered | Offer cancelled; coach notified |
| Goalie backs out | Only goalie-eligible substitutes count toward "Needs Substitute" for the goalie slot |
| Junior player response | Linked guardian may respond on the player's behalf |
| Coach manually adds player | Override allowed; audit entry required |
| Player already selected | Cannot also enroll as a substitute |
| Player from the wrong team | Not eligible unless cross-team borrowing is enabled |
| Roster locked | No player changes until coach/admin unlocks |
| Game cancelled | Substitute enrollments cancelled |
| No substitutes enrolled | Show open slot clearly; notify coach |
