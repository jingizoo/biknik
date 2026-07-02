"""iCal (RFC 5545) subscription feeds, scoped by a bearer token (#82).

Given a feed token bound to one actor — a team, an official, or a player — build
a VCALENDAR of that actor's games. Feeds are public-safe fixtures only (teams,
time, rink, division); no roster/player-name details leak into a team or player
calendar. The official feed adds only the official's own assignment role.

Pure and deterministic: the store is passed in and the ``now`` used for DTSTAMP
is injected, so output is reproducible and testable.
"""

import hashlib
import secrets
from datetime import timedelta, timezone

ACTOR_TYPES = ("team", "official", "player")
# Fallback event length when a game has no explicit end_time.
DEFAULT_GAME_MINUTES = 90


def new_feed_token() -> str:
    """A fresh, high-entropy raw feed token (only its hash is persisted)."""
    return secrets.token_urlsafe(24)


def hash_feed_token(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def games_for_actor(store, actor_type: str, actor_ref: str):
    """The games that belong on ``actor_ref``'s calendar.

    - team   → published games the team plays (home or away);
    - player → the player's team's published games;
    - official → games the official is actively assigned to (published or not,
      since the token already scopes access to that official).
    """
    if actor_type == "team":
        return [g for g in store.all_games()
                if g.published and actor_ref in (g.home_team_id, g.away_team_id)]
    if actor_type == "player":
        player = store.get_player(actor_ref)
        if player is None:
            return []
        return [g for g in store.all_games()
                if g.published
                and player.team_id in (g.home_team_id, g.away_team_id)]
    if actor_type == "official":
        game_ids = {a.game_id for a in store.assignments_for_official(actor_ref)
                    if a.status.is_active}
        return [g for g in store.all_games() if g.id in game_ids]
    return []


def _ics_dt(dt) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _escape(text: str) -> str:
    # RFC 5545 text escaping for SUMMARY/LOCATION/DESCRIPTION.
    return (str(text or "").replace("\\", "\\\\").replace(";", r"\;")
            .replace(",", r"\,").replace("\n", r"\n"))


def _fold(line: str) -> str:
    # RFC 5545 recommends folding lines longer than 75 octets.
    if len(line) <= 75:
        return line
    out, i = [line[:75]], 75
    while i < len(line):
        out.append(" " + line[i:i + 74])
        i += 74
    return "\r\n".join(out)


def build_ics(store, actor_type: str, actor_ref: str, now,
              calendar_name: str = "Hockey Scheduler") -> str:
    """Render a VCALENDAR string for the actor's games (CRLF line endings)."""
    def team_name(tid):
        t = store.get_team(tid) if tid else None
        return t.name if t else (tid or "TBD")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Hockey Scheduler//Calendar Feed//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(calendar_name)}",
    ]
    games = sorted(games_for_actor(store, actor_type, actor_ref),
                   key=lambda g: g.start_time)
    for g in games:
        start = g.start_time
        end = g.end_time or (start + timedelta(minutes=DEFAULT_GAME_MINUTES))
        summary = f"{team_name(g.home_team_id)} vs {team_name(g.away_team_id)}"
        div = store.get_division(g.division_id) if g.division_id else None
        desc_parts = []
        if div:
            desc_parts.append(f"Division: {div.name}")
        if actor_type == "official":
            roles = [a.role.value for a in store.assignments_for_game(g.id)
                     if a.official_id == actor_ref and a.status.is_active]
            if roles:
                desc_parts.append("Role: " + ", ".join(roles))
        location = g.rink or ""
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{g.id}@hockey-scheduler")
        lines.append(f"DTSTAMP:{_ics_dt(now)}")
        lines.append(f"DTSTART:{_ics_dt(start)}")
        lines.append(f"DTEND:{_ics_dt(end)}")
        lines.append("SUMMARY:" + _escape(summary))
        if location:
            lines.append("LOCATION:" + _escape(location))
        if desc_parts:
            lines.append("DESCRIPTION:" + _escape("; ".join(desc_parts)))
        lines.append("STATUS:" + ("CANCELLED" if g.cancelled else "CONFIRMED"))
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(ln) for ln in lines) + "\r\n"
