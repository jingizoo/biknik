-- 001_initial: core schema (everything up to and including #73).
--
-- Portable across SQLite and PostgreSQL: TEXT/INTEGER columns only, datetimes
-- as ISO-8601 text, booleans as 0/1, dict payloads as JSON text. Every object
-- is CREATE ... IF NOT EXISTS so this migration is safe to re-run and safe to
-- adopt on a database that predates the numbered-migration system (#75).

-- Monotonic id counters (prefix -> last value), used by next_id().
CREATE TABLE IF NOT EXISTS counters (prefix TEXT PRIMARY KEY, value INTEGER);

-- League / season / division structure.
CREATE TABLE IF NOT EXISTS leagues (id TEXT PRIMARY KEY, name TEXT, country TEXT, timezone TEXT);
CREATE TABLE IF NOT EXISTS seasons (id TEXT PRIMARY KEY, league_id TEXT, name TEXT, start_date TEXT, end_date TEXT);
CREATE TABLE IF NOT EXISTS divisions (id TEXT PRIMARY KEY, season_id TEXT, name TEXT, age_group TEXT);
CREATE TABLE IF NOT EXISTS clubs (id TEXT PRIMARY KEY, name TEXT, country TEXT);
CREATE TABLE IF NOT EXISTS teams (id TEXT PRIMARY KEY, name TEXT, division TEXT, club_id TEXT, division_id TEXT);
CREATE TABLE IF NOT EXISTS players (id TEXT PRIMARY KEY, team_id TEXT, name TEXT, position TEXT, shoots TEXT, jersey_number INTEGER, is_active INTEGER, guardian_person_id TEXT);

-- Venues / rinks / ice slots.
CREATE TABLE IF NOT EXISTS venues (id TEXT PRIMARY KEY, name TEXT, address TEXT, timezone TEXT);
CREATE TABLE IF NOT EXISTS rinks (id TEXT PRIMARY KEY, venue_id TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS ice_slots (id TEXT PRIMARY KEY, rink_id TEXT, start_time TEXT, end_time TEXT, slot_type TEXT, status TEXT);

-- Games, rosters, availability, substitutes.
CREATE TABLE IF NOT EXISTS games (id TEXT PRIMARY KEY, home_team_id TEXT, start_time TEXT, target_goalies INTEGER, target_skaters INTEGER, max_skaters INTEGER, away_team_id TEXT, rink TEXT, end_time TEXT, roster_lock_time TEXT, locked INTEGER, cancelled INTEGER, published INTEGER, season_id TEXT, division_id TEXT, ice_slot_id TEXT);
CREATE TABLE IF NOT EXISTS game_roster_entries (id TEXT PRIMARY KEY, game_id TEXT, player_id TEXT, roster_role TEXT, selection_source TEXT, status TEXT, selected_at TEXT, updated_at TEXT, selected_by TEXT);
CREATE TABLE IF NOT EXISTS game_availability (id TEXT PRIMARY KEY, game_id TEXT, player_id TEXT, availability_status TEXT, response_source TEXT, responded_at TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS substitute_enrollments (id TEXT PRIMARY KEY, game_id TEXT, player_id TEXT, position TEXT, status TEXT, enrolled_at TEXT, priority_rank INTEGER, offered_at TEXT, offer_expires_at TEXT, accepted_at TEXT, declined_at TEXT);

-- Audit + notification events.
CREATE TABLE IF NOT EXISTS audit_logs (id TEXT PRIMARY KEY, game_id TEXT, action TEXT, at TEXT, actor_id TEXT, subject_player_id TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS notification_events (id TEXT PRIMARY KEY, game_id TEXT, type TEXT, audience TEXT, message TEXT, at TEXT, subject_player_id TEXT);
CREATE TABLE IF NOT EXISTS setup_audit_logs (id TEXT PRIMARY KEY, action TEXT, entity_type TEXT, entity_id TEXT, at TEXT, actor_id TEXT, detail TEXT);

-- Officials + assignments.
CREATE TABLE IF NOT EXISTS officials (id TEXT PRIMARY KEY, name TEXT, home_club_id TEXT, is_active INTEGER);
CREATE TABLE IF NOT EXISTS official_assignments (id TEXT PRIMARY KEY, game_id TEXT, official_id TEXT, role TEXT, status TEXT, assigned_at TEXT, responded_at TEXT, assigned_by TEXT, note TEXT);

-- Results.
CREATE TABLE IF NOT EXISTS game_results (id TEXT PRIMARY KEY, game_id TEXT, home_score INTEGER, away_score INTEGER, status TEXT, recorded_by TEXT, recorded_at TEXT, approved_by TEXT, approved_at TEXT);

-- Notification feed + delivery + contacts.
CREATE TABLE IF NOT EXISTS notifications_feed (id TEXT PRIMARY KEY, kind TEXT, audience TEXT, title TEXT, message TEXT, at TEXT, audience_ref TEXT, game_id TEXT, assignment_id TEXT);
CREATE TABLE IF NOT EXISTS notification_recipients (id TEXT PRIMARY KEY, notification_id TEXT, actor_key TEXT, read_at TEXT);
CREATE TABLE IF NOT EXISTS notification_deliveries (id TEXT PRIMARY KEY, notification_id TEXT, channel TEXT, status TEXT, attempts INTEGER, last_error TEXT, sent_at TEXT, recipient_ref TEXT, destination TEXT);
CREATE TABLE IF NOT EXISTS contact_destinations (id TEXT PRIMARY KEY, recipient_ref TEXT, channel TEXT, destination TEXT, label TEXT);
CREATE TABLE IF NOT EXISTS device_tokens (id TEXT PRIMARY KEY, recipient_ref TEXT, provider TEXT, token TEXT, label TEXT, active INTEGER);

-- Login accounts.
CREATE TABLE IF NOT EXISTS user_accounts (id TEXT PRIMARY KEY, username TEXT, password_hash TEXT, role TEXT, created_at TEXT, scope TEXT, active INTEGER);

-- Indexes for the common lookups.
CREATE INDEX IF NOT EXISTS ix_ice_slots_rink ON ice_slots(rink_id, start_time);
CREATE INDEX IF NOT EXISTS ix_games_slot ON games(ice_slot_id);
CREATE INDEX IF NOT EXISTS ix_games_teams ON games(home_team_id, away_team_id);
CREATE INDEX IF NOT EXISTS ix_players_team ON players(team_id);
CREATE INDEX IF NOT EXISTS ix_roster_game ON game_roster_entries(game_id, player_id);
CREATE INDEX IF NOT EXISTS ix_subs_game ON substitute_enrollments(game_id, player_id);
CREATE INDEX IF NOT EXISTS ix_avail_game ON game_availability(game_id, player_id);
CREATE INDEX IF NOT EXISTS ix_off_assign_game ON official_assignments(game_id);
CREATE INDEX IF NOT EXISTS ix_off_assign_official ON official_assignments(official_id);
CREATE INDEX IF NOT EXISTS ix_game_results_game ON game_results(game_id);
CREATE INDEX IF NOT EXISTS ix_notifs_feed_aud ON notifications_feed(audience, audience_ref);
CREATE INDEX IF NOT EXISTS ix_notif_recips_actor ON notification_recipients(actor_key);
CREATE INDEX IF NOT EXISTS ix_notif_deliv_status ON notification_deliveries(status);
CREATE INDEX IF NOT EXISTS ix_notif_deliv_notif ON notification_deliveries(notification_id);
CREATE INDEX IF NOT EXISTS ix_contacts_ref ON contact_destinations(recipient_ref, channel);
CREATE INDEX IF NOT EXISTS ix_device_tokens_ref ON device_tokens(recipient_ref, active);
CREATE UNIQUE INDEX IF NOT EXISTS ix_user_accounts_username ON user_accounts(username);
