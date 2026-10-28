ALTER TABLE courses ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;
ALTER TABLE courses ADD COLUMN featured INTEGER NOT NULL DEFAULT 0;
CREATE TABLE publishing_suspensions(player_id INTEGER PRIMARY KEY REFERENCES players(id), reason TEXT NOT NULL);
CREATE TABLE moderation_actions(id INTEGER PRIMARY KEY, action TEXT NOT NULL, target TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE verification_runs(
 id TEXT PRIMARY KEY, slot_id TEXT NOT NULL REFERENCES slots(id), player_id INTEGER NOT NULL,
 server_id TEXT NOT NULL, presence_id TEXT NOT NULL, revision INTEGER NOT NULL,
 content_hash TEXT NOT NULL, rules_version INTEGER NOT NULL, created_at REAL NOT NULL,
 expires_at REAL NOT NULL, finished_at REAL, elapsed REAL, proof_json TEXT
);
CREATE TABLE course_versions(
 id TEXT PRIMARY KEY, course_id TEXT NOT NULL REFERENCES courses(id), version_number INTEGER NOT NULL,
 layout_json TEXT NOT NULL, content_hash TEXT NOT NULL, verification_id TEXT NOT NULL REFERENCES verification_runs(id),
 title TEXT NOT NULL, description TEXT NOT NULL, published_at REAL NOT NULL,
 UNIQUE(course_id,version_number)
);
CREATE TABLE runs(
 id TEXT PRIMARY KEY, course_id TEXT NOT NULL REFERENCES courses(id), version_id TEXT NOT NULL REFERENCES course_versions(id),
 player_id INTEGER NOT NULL REFERENCES players(id), server_id TEXT NOT NULL, presence_id TEXT NOT NULL,
 started_at REAL NOT NULL, finished_at REAL, elapsed REAL, deaths INTEGER, proof_json TEXT
);
CREATE TABLE visit_events(course_id TEXT NOT NULL REFERENCES courses(id), player_id INTEGER NOT NULL REFERENCES players(id),
 day INTEGER NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(course_id,player_id,day));
CREATE TABLE votes(course_id TEXT REFERENCES courses(id), player_id INTEGER REFERENCES players(id),
 created_at REAL NOT NULL, PRIMARY KEY(course_id,player_id));
CREATE TABLE favourites(course_id TEXT REFERENCES courses(id), player_id INTEGER REFERENCES players(id),
 created_at REAL NOT NULL, PRIMARY KEY(course_id,player_id));
CREATE TABLE reports(id TEXT PRIMARY KEY, course_id TEXT NOT NULL REFERENCES courses(id), version_id TEXT NOT NULL,
 player_id INTEGER NOT NULL REFERENCES players(id), category TEXT NOT NULL, created_at REAL NOT NULL,
 status TEXT NOT NULL DEFAULT 'open', resolution TEXT);
CREATE TABLE discovery_cursors(id TEXT PRIMARY KEY, server_id TEXT NOT NULL, presence_id TEXT NOT NULL,
 remaining_json TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE INDEX public_courses ON courses(visibility,hidden,deleted_at);
CREATE INDEX version_course ON course_versions(course_id,version_number);
CREATE INDEX verification_slot ON verification_runs(slot_id,created_at);
CREATE INDEX run_player ON runs(player_id,started_at);
CREATE INDEX run_course ON runs(course_id,started_at);
CREATE INDEX visits_week ON visit_events(created_at,course_id);
CREATE INDEX report_queue ON reports(status,created_at);
CREATE INDEX cursor_expiry ON discovery_cursors(expires_at);
INSERT INTO schema_migrations VALUES(2);
