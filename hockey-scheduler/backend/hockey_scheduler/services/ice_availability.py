"""Ice Availability Builder engine (#158).

Pure, deterministic generation of recurring Game ice windows from a weekly
availability template, over a date range, in a Program-local timezone. There is
NO I/O here: the store-aware preview/commit orchestration (rink expansion,
SeasonVenueAccess checks, collision/duplicate classification, audit, and the
actual writes) lives in ``SetupService``, next to ``create_ice_slot`` and the
rinks/ice-slots importer whose idempotency this mirrors.

Model (minimal-buffer scope for #158; the deeper turnover/curfew model is #277):
a generated slot's ``[start, end]`` is its PLAYABLE game window
(``playable_minutes``) — a clean game window the existing scheduler already
consumes. Consecutive slots on a day are spaced by ``playable + turnover``, so a
turnover gap sits between games. Reserved facility time (the contracted window)
is surfaced separately for display; it is not persisted per slot here.

All wall-clock arithmetic is done in the Program timezone and then converted to
the UTC instants the store holds, exactly like ``parse_season_boundary``.
"""

from datetime import datetime, timedelta, timezone

from ..domain.errors import ValidationError

# date.weekday(): Monday=0 .. Sunday=6.
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday"]

# Guard rails so a fat-fingered range can never generate an unbounded preview.
MAX_RANGE_DAYS = 400
MAX_WINDOWS = 2000


def parse_hhmm(value, field_name):
    """Parse a ``"HH:MM"`` 24-hour local time into a ``(hour, minute)`` tuple.

    Raises a field-level ``ValidationError`` (``invalid_<field_name>``) on any
    malformed value so the facade returns a structured error, never a raw
    ``ValueError`` across the boundary.
    """
    field = {"reason": f"invalid_{field_name}", "field": field_name}
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a HH:MM time string.", field)
    parts = value.strip().split(":")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValidationError(
            f"Invalid {field_name}: {value!r}. Expected HH:MM (24-hour).", field)
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        raise ValidationError(
            f"Invalid {field_name}: {value!r}. Hours 0-23, minutes 0-59.", field)
    return hour, minute


def _resolve_local_to_utc(naive, tz):
    """Resolve a naive local wall-clock ``datetime`` to a UTC instant under DST.

    ``datetime(..., tzinfo=ZoneInfo)`` is silently wrong across a DST transition:
    a spring-forward gap time (e.g. America/Toronto 02:30 on the March change) has
    NO real instant, and a fall-back repeated hour has two — attaching the zone and
    advancing with ``timedelta`` can persist a zero-duration slot or misstate a
    duration. Resolve explicitly instead. Returns ``(utc, status)``:

      * ``"ok"`` — the wall time exists unambiguously;
      * ``"ambiguous"`` — a fall-back repeated hour; the EARLIER instant (``fold=0``,
        before the clocks go back) is chosen deterministically so preview and
        commit always agree;
      * ``"nonexistent"`` — a spring-forward gap; ``utc`` is ``None`` and the caller
        skips the day with a structured reason.
    """
    local = naive.replace(tzinfo=tz)                       # fold=0
    utc = local.astimezone(timezone.utc)
    # A gap instant does not round-trip: converting to UTC and back lands on a
    # DIFFERENT wall time than the one asked for.
    if utc.astimezone(tz).replace(tzinfo=None) != naive:
        return None, "nonexistent"
    # A repeated hour is ambiguous when its two folds carry different UTC offsets.
    if local.utcoffset() != local.replace(fold=1).utcoffset():
        return utc, "ambiguous"
    return utc, "ok"


def plan_ice_windows(*, weekday_windows, start_date, end_date,
                     playable_minutes, turnover_minutes, exclusion_dates, tz):
    """Generate the per-rink game-ice windows a recurring template implies.

    Rink-agnostic: the same windows apply to every selected rink, so the caller
    expands the result across rinks and classifies each against existing
    inventory.

    Arguments:
      * ``weekday_windows``: ``{weekday_int: ((sh, sm), (eh, em))}`` — which
        weekdays are active and each one's local start/end wall-clock time.
      * ``start_date`` / ``end_date``: inclusive ``date`` range to generate over.
      * ``playable_minutes``: playable game length (int >= 1).
      * ``turnover_minutes``: gap between consecutive games (int >= 0).
      * ``exclusion_dates``: a ``set`` of ``date`` to skip (recorded, never made).
      * ``tz``: the Program ``tzinfo`` all local times are anchored in.

    Returns a dict::

        {"windows": [{date, start(UTC), end(UTC), reserved_end(UTC),
                      start_local, end_local}],
         "skipped_dates": [{date, reason: "exclusion"}],
         "too_short": [{date, window_minutes}],   # a selected day too short for
                                                   # even one playable game
         "dst_skipped": [{date, reason: "dst_nonexistent_window", boundary}],
                                                   # a window start/end in a
                                                   # spring-forward gap (no instant)
         "dst_ambiguous": [{date, boundary, reason: "dst_ambiguous_fold0"}],
                                                   # a fall-back repeated boundary;
                                                   # the earlier fold was used
         "reserved_minutes": int,          # single rink, sum of active windows
         "playable_minutes_total": int,    # single rink, sum of game lengths
         "game_days": int}

    Deterministic: windows come out sorted by (date, start).
    """
    if not isinstance(playable_minutes, int) or isinstance(playable_minutes, bool) \
            or playable_minutes < 1:
        raise ValidationError(
            "playable_minutes must be an integer >= 1.",
            {"reason": "invalid_playable_minutes", "field": "playable_minutes"})
    if not isinstance(turnover_minutes, int) or isinstance(turnover_minutes, bool) \
            or turnover_minutes < 0:
        raise ValidationError(
            "turnover_minutes must be an integer >= 0.",
            {"reason": "invalid_turnover_minutes", "field": "turnover_minutes"})
    if not weekday_windows:
        raise ValidationError(
            "Select at least one weekday.",
            {"reason": "no_weekdays", "field": "weekdays"})
    for wd, win in weekday_windows.items():
        if wd not in range(7):
            raise ValidationError(
                f"Invalid weekday {wd!r}; use 0 (Monday) through 6 (Sunday).",
                {"reason": "invalid_weekday", "field": "weekdays"})
        (sh, sm), (eh, em) = win
        if (eh, em) <= (sh, sm):
            raise ValidationError(
                f"{WEEKDAY_NAMES[wd]} end time must be after its start time.",
                {"reason": "window_end_before_start", "field": "weekdays"})
    if end_date < start_date:
        raise ValidationError(
            "end_date must be on or after start_date.",
            {"reason": "range_end_before_start", "field": "end_date"})
    if (end_date - start_date).days > MAX_RANGE_DAYS:
        raise ValidationError(
            f"Date range is too long (max {MAX_RANGE_DAYS} days). Narrow the "
            "range and generate in stages.",
            {"reason": "range_too_long", "field": "end_date"})

    step = playable_minutes + turnover_minutes
    playable = timedelta(minutes=playable_minutes)
    step_td = timedelta(minutes=step)
    windows, skipped, too_short = [], [], []
    dst_skipped, dst_ambiguous = [], []
    reserved_minutes = 0
    playable_total = 0
    game_days = 0

    day = start_date
    one_day = timedelta(days=1)
    while day <= end_date:
        win = weekday_windows.get(day.weekday())
        if win is None:
            day += one_day
            continue
        if day in exclusion_dates:
            skipped.append({"date": day.isoformat(), "reason": "exclusion"})
            day += one_day
            continue
        (sh, sm), (eh, em) = win
        # Resolve the window's local start/end to real UTC instants (DST-safe).
        # A boundary in a spring-forward gap has no instant -> skip the day with a
        # structured reason; a fall-back repeated hour deterministically uses the
        # earlier fold. Advancing the cursor in UTC below then guarantees real
        # durations — never a zero/negative/duplicate slot across a transition, and
        # a truthful window_minutes (a spring-forward day is an hour shorter, a
        # fall-back day an hour longer).
        start_utc, start_status = _resolve_local_to_utc(
            datetime(day.year, day.month, day.day, sh, sm), tz)
        end_utc, end_status = _resolve_local_to_utc(
            datetime(day.year, day.month, day.day, eh, em), tz)
        if start_utc is None or end_utc is None:
            dst_skipped.append({
                "date": day.isoformat(), "reason": "dst_nonexistent_window",
                "boundary": "start" if start_utc is None else "end"})
            day += one_day
            continue
        for boundary, status in (("start", start_status), ("end", end_status)):
            if status == "ambiguous":
                dst_ambiguous.append({
                    "date": day.isoformat(), "boundary": boundary,
                    "reason": "dst_ambiguous_fold0"})
        window_minutes = int((end_utc - start_utc).total_seconds() // 60)
        if window_minutes <= 0:
            too_short.append({"date": day.isoformat(),
                              "window_minutes": max(window_minutes, 0)})
            day += one_day
            continue

        cursor = start_utc
        day_count = 0
        while cursor + playable <= end_utc:
            playable_end = cursor + playable
            reserved_end = min(cursor + step_td, end_utc)
            windows.append({
                "date": day.isoformat(),
                "start": cursor,
                "end": playable_end,
                "reserved_end": reserved_end,
                "start_local": cursor.astimezone(tz).isoformat(),
                "end_local": playable_end.astimezone(tz).isoformat(),
            })
            playable_total += playable_minutes
            day_count += 1
            if len(windows) > MAX_WINDOWS:
                raise ValidationError(
                    f"This template would generate more than {MAX_WINDOWS} "
                    "slots. Narrow the range, weekdays, or window.",
                    {"reason": "too_many_slots", "field": "end_date"})
            cursor += step_td

        if day_count:
            reserved_minutes += window_minutes
            game_days += 1
        else:
            # A selected, non-excluded day whose contracted window cannot host a
            # single playable game — surfaced so the operator can widen it.
            too_short.append({"date": day.isoformat(),
                              "window_minutes": window_minutes})
        day += one_day

    return {
        "windows": windows,
        "skipped_dates": skipped,
        "too_short": too_short,
        "dst_skipped": dst_skipped,
        "dst_ambiguous": dst_ambiguous,
        "reserved_minutes": reserved_minutes,
        "playable_minutes_total": playable_total,
        "game_days": game_days,
    }


__all__ = ["plan_ice_windows", "parse_hhmm", "WEEKDAY_NAMES",
           "MAX_RANGE_DAYS", "MAX_WINDOWS"]
