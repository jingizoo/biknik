# Season date boundaries — timezone semantics (#272)

A season's `start_date` / `end_date` are primarily **calendar dates**. League
offices and canonical imports enter values like `2026-09-15`, not a
timezone-aware instant. This document defines exactly how those values are
accepted, normalized, stored, and displayed.

## Accepted input forms

`create_season` accepts either form for `start_date` and `end_date` (both
optional; `null`/empty means "unset"):

| Input | Example | Meaning |
| --- | --- | --- |
| **Date-only** `YYYY-MM-DD` | `2026-09-15` | A calendar day. Anchored to **local midnight (00:00) in the Program's timezone**, then stored as the equivalent UTC instant. |
| **Timezone-aware ISO-8601** | `2026-09-15T00:00:00-04:00`, `2026-09-15T04:00:00+00:00` | An exact instant. Stored as that instant converted to UTC — no drift. |

A **naive timestamp that carries a time** (e.g. `2026-09-15T18:30:00`, no
offset) is **rejected**: a time without a zone is ambiguous. Use a bare
`YYYY-MM-DD` for a calendar date, or include an offset for an instant.

## The timezone anchor

There is no per-Season timezone. A Season belongs to a **Program**, and
`Program.timezone` (an IANA name such as `America/Chicago`, default `UTC`) is the
anchor used to interpret a date-only boundary. `create_program` validates the
timezone is a real IANA zone (`invalid_timezone` field error) so the anchor is
always trustworthy for programs created going forward; a legacy/unknown zone
falls back to UTC when a boundary is interpreted, so season creation never fails
on stale data.

Because a date-only value is anchored to local midnight and stored as UTC, it
round-trips to the **same calendar day** when displayed in the Program's
timezone. Displays that render season boundaries should therefore format them in
the Program timezone; rendering the raw UTC instant in a different zone can show
an adjacent day (e.g. `2026-09-15` in `Asia/Tokyo` is `2026-09-14T15:00:00Z`).

## Normalization examples

Program timezone `America/New_York` (UTC−04:00 on 2026-09-15, EDT):

- `start_date = "2026-09-15"` → `2026-09-15T04:00:00+00:00` (midnight EDT).
- Displayed back in `America/New_York` → `2026-09-15`. ✔

Program timezone `Asia/Tokyo` (UTC+09:00):

- `start_date = "2026-09-15"` → `2026-09-14T15:00:00+00:00` (midnight JST).
- Displayed back in `Asia/Tokyo` → `2026-09-15`. ✔

DST is handled by the zone database: a date-only boundary always resolves to
00:00 wall-clock on that day in the Program zone, whatever the offset that day.

## Validation

- Unparseable / real-calendar-invalid input → field error `invalid_start_date` /
  `invalid_end_date` (`field` = the offending field), before any write.
- `end_date` before `start_date` → field error `end_before_start`
  (`field: "end_date"`). Equal start/end is allowed (a one-day season).

## Stored & API representation

`Season.start_date` / `end_date` are timezone-aware UTC `datetime`s, persisted as
ISO-8601 text and serialized on the wire as ISO-8601 strings (or `null`) —
unchanged from before #272. **Existing stored instants are never rewritten**;
this change only widens what `create_season` accepts as input.

## Import path

The canonical hierarchy `competition` sheet carries two **optional** trailing
columns, `season_start` and `season_end`, accepting the same forms as manual
setup. Old templates without the columns still import (absent → unset). Import
preview and commit use the **same** `parse_season_boundary` and the **effective
Program timezone**: if the upload's `programs` sheet supplies (or changes) the
Program's timezone, date-only Season values are normalized with that uploaded
zone; only when no Program row is supplied is the stored Program timezone used
— preview and commit resolve this identically.

- **Repeated `competition` rows** for one `season_code` are compared **after
  normalization**: two non-blank forms resolving to the same UTC instant agree;
  a blank cell is unspecified (never a conflict); two different normalized
  non-blank values are rejected (`inconsistent_season_dates`).
- **Blank re-import preserves** a stored boundary — a blank cell never clears an
  existing Season's start/end. A supplied value equal to the stored instant is a
  no-op (no update/audit).
- The **final merged pair** is range-checked: a supplied side is compared against
  the preserved opposite side, and a reversed final range is rejected
  (`season_end_before_start`). Invalid values are row/field-specific
  (`invalid_season_start` / `invalid_season_end`) and produce **zero writes**
  (the whole batch rolls back).
- The uploaded `programs.timezone` is itself validated with the same
  `invalid_timezone` contract, so an unknown zone is never persisted.

**Changing a Program's timezone.** Stored Season instants are never rewritten,
and every Season display formats them in the Program's *current* timezone — so
changing a Program's timezone would silently shift the calendar day of its
already-dated Seasons. With no per-Season date/timezone provenance, the import
**rejects a timezone change for a Program that has any Season with a stored
boundary** (`program_timezone_in_use`, `field="timezone"`, zero writes).
Same-zone/idempotent imports pass, and a timezone change is allowed only when
the Program has no dated Seasons. Changing the timezone of a Program with dated
Seasons needs an explicit product decision plus durable per-Season provenance.

## Scope

`parse_season_boundary` is the single entry point for season boundaries, shared
by `create_season` (manual setup) and the hierarchy import. Rollover/copy-forward
reuses an existing target season's boundaries rather than recomputing them. Any
future season-date writer — a season editor or registration windows — must call
`parse_season_boundary` so the contract stays uniform.
