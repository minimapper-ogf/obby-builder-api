CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY);
INSERT INTO schema_migrations VALUES(1);
CREATE TABLE server_sessions(
 id TEXT PRIMARY KEY, token_digest TEXT UNIQUE NOT NULL, game_id INTEGER NOT NULL,
 display_name TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
 absolute_expiry REAL NOT NULL, heartbeat_at REAL NOT NULL, player_count INTEGER NOT NULL DEFAULT 0,
 revoked_at REAL
);
CREATE TABLE players(id INTEGER PRIMARY KEY, created_at REAL NOT NULL);
CREATE TABLE wallets(player_id INTEGER PRIMARY KEY REFERENCES players(id), balance INTEGER NOT NULL CHECK(balance>=0));
CREATE TABLE inventory(player_id INTEGER REFERENCES players(id), piece_type TEXT NOT NULL,
 quantity INTEGER NOT NULL CHECK(quantity>=0), PRIMARY KEY(player_id,piece_type));
CREATE TABLE player_sessions(id TEXT PRIMARY KEY, player_id INTEGER NOT NULL REFERENCES players(id),
 server_id TEXT NOT NULL REFERENCES server_sessions(id), expires_at REAL NOT NULL, closed_at REAL,
 UNIQUE(server_id,player_id));
CREATE TABLE courses(id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES players(id),
 title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', current_published_version TEXT,
 visibility TEXT NOT NULL DEFAULT 'draft', deleted_at REAL);
CREATE TABLE slots(id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES players(id),
 slot_index INTEGER NOT NULL CHECK(slot_index BETWEEN 1 AND 5), course_id TEXT UNIQUE NOT NULL REFERENCES courses(id),
 UNIQUE(owner_id,slot_index));
CREATE TABLE drafts(slot_id TEXT PRIMARY KEY REFERENCES slots(id), layout_json TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0, content_hash TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE edit_leases(slot_id TEXT PRIMARY KEY REFERENCES slots(id), id TEXT UNIQUE NOT NULL,
 server_id TEXT NOT NULL REFERENCES server_sessions(id), presence_id TEXT NOT NULL REFERENCES player_sessions(id),
 expires_at REAL NOT NULL);
CREATE TABLE idempotency_records(server_id TEXT REFERENCES server_sessions(id), request_id TEXT NOT NULL,
 request_digest TEXT NOT NULL, response_json TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(server_id,request_id));
CREATE INDEX session_expiry ON server_sessions(expires_at);
CREATE INDEX presence_expiry ON player_sessions(expires_at);
CREATE INDEX lease_expiry ON edit_leases(expires_at);
CREATE INDEX owner_slots ON slots(owner_id,slot_index);
CREATE INDEX idempotency_retention ON idempotency_records(created_at);
