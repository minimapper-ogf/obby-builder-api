"""Transactional persistence service. All identity assertions come from trusted game servers."""
from contextlib import contextmanager
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit, parse_qs

from layouts import STOCK, canonical, content_hash, initial_layout, normalize_empty_tables, validate


class ApiError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def fail(status, code, message):
    raise ApiError(status, code, message)


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", value)


class Service:
    def __init__(self, database, bootstrap_secrets, game_id, clock=time.time):
        if not bootstrap_secrets or any(len(secret) < 32 for secret in bootstrap_secrets):
            raise ValueError("Bootstrap credentials must have at least 32 characters")
        self.database, self.secrets, self.game_id, self.clock = str(database), bootstrap_secrets, game_id, clock
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise RuntimeError("Unsupported database schema version")
            for target, filename in ((1, "001_persistence.sql"), (2, "002_publication.sql")):
                if version < target:
                    migration = (Path(__file__).parent / "migrations" / filename).read_text()
                    db.executescript("BEGIN IMMEDIATE;\n" + migration + f"\nPRAGMA user_version={target}; COMMIT;")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def auth(self, db, authorization, now):
        if not authorization.startswith("Bearer "):
            fail(401, "session_invalid", "Server session required")
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        row = db.execute("SELECT * FROM server_sessions WHERE token_digest=?", (digest,)).fetchone()
        if not row or row["revoked_at"] is not None or row["expires_at"] <= now or row["absolute_expiry"] <= now:
            fail(401, "session_expired", "Server session is unavailable")
        return row

    def presence(self, db, server, presence_id, now):
        row = db.execute("SELECT * FROM player_sessions WHERE id=?", (presence_id,)).fetchone()
        if not row or row["server_id"] != server["id"] or row["closed_at"] is not None or row["expires_at"] <= now:
            fail(403, "presence_invalid", "Connected player presence required")
        return row

    def slot(self, db, slot_id, presence):
        row = db.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
        if not row or row["owner_id"] != presence["player_id"]:
            fail(403, "slot_owner", "This course slot is unavailable")
        return row

    def draft_payload(self, db, slot, draft):
        verified = db.execute("SELECT id FROM verification_runs WHERE slot_id=? AND revision=? AND content_hash=? AND rules_version=1 AND finished_at IS NOT NULL ORDER BY finished_at DESC,id LIMIT 1", (slot["id"], draft["revision"], draft["content_hash"])).fetchone()
        return dict(course_id=slot["course_id"], revision=draft["revision"], layout=json.loads(draft["layout_json"]), content_hash=draft["content_hash"], updated_at=draft["updated_at"], verification_id=verified["id"] if verified else None)

    def bootstrap(self, db, player_id, now, include_drafts=False):
        if not db.execute("SELECT 1 FROM players WHERE id=?", (player_id,)).fetchone():
            db.execute("INSERT INTO players VALUES(?,?)", (player_id, now))
            db.execute("INSERT INTO wallets VALUES(?,0)", (player_id,))
            db.executemany("INSERT INTO inventory VALUES(?,?,?)", [(player_id, kind, amount) for kind, amount in STOCK.items()])
            for index in range(1, 6):
                course_id, slot_id = "OB-"+secrets.token_hex(6).upper(), secrets.token_hex(16)
                db.execute("INSERT INTO courses(id,owner_id,title) VALUES(?,?,?)", (course_id, player_id, "Course "+str(index)))
                db.execute("INSERT INTO slots VALUES(?,?,?,?)", (slot_id, player_id, index, course_id))
                layout = initial_layout()
                db.execute("INSERT INTO drafts VALUES(?,?,0,?,?)", (slot_id, canonical(layout), content_hash(layout), now))
        stock = dict(db.execute("SELECT piece_type,quantity FROM inventory WHERE player_id=?", (player_id,)).fetchall())
        slots = [dict(row) for row in db.execute("SELECT s.id,s.slot_index,s.course_id,c.title,c.description,c.current_published_version,c.visibility,d.revision,d.updated_at FROM slots s JOIN courses c ON c.id=s.course_id JOIN drafts d ON d.slot_id=s.id WHERE s.owner_id=? ORDER BY s.slot_index", (player_id,))]
        if include_drafts:
            for slot in slots:
                draft = db.execute("SELECT * FROM drafts WHERE slot_id=?", (slot["id"],)).fetchone()
                slot["draft"] = self.draft_payload(db, slot, draft)
        return dict(player_id=player_id, inventory=stock, balance=db.execute("SELECT balance FROM wallets WHERE player_id=?", (player_id,)).fetchone()[0],
                    slots=slots, object_limit=150, entitlements=[], schema_version=1, rules_version=1)

    def dispatch(self, method, target, authorization="", data=None):
        if data is None:
            data = {}
        if not isinstance(data, dict):
            fail(400, "invalid_body", "A JSON object is required")
        url = urlsplit(target)
        path = url.path.rstrip("/")
        query = parse_qs(url.query)
        now = self.clock()
        if method == "GET" and path == "/health":
            return {"ok": True, "schema_version": 2}
        with self.transaction() as db:
            if method == "POST" and path == "/v1/server-sessions":
                supplied = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
                if not any(hmac.compare_digest(supplied, secret) for secret in self.secrets):
                    fail(401, "bootstrap_invalid", "Registration credential is invalid")
                if type(data.get("game_id")) is not int or data["game_id"] != self.game_id or type(data.get("protocol_version")) is not int or data["protocol_version"] != 1:
                    fail(403, "game_invalid", "Unsupported game or protocol")
                name = data.get("display_name", "Obby server")
                if not isinstance(name, str) or len(name) > 80:
                    fail(400, "invalid_name", "Invalid server name")
                if db.execute("SELECT count(*) FROM server_sessions WHERE created_at>?", (now-60,)).fetchone()[0] >= 30:
                    fail(429, "registration_limited", "Please retry server registration later")
                sid, token = secrets.token_hex(16), secrets.token_urlsafe(32)
                db.execute("INSERT INTO server_sessions(id,token_digest,game_id,display_name,created_at,expires_at,absolute_expiry,heartbeat_at) VALUES(?,?,?,?,?,?,?,?)",
                           (sid, hashlib.sha256(token.encode()).hexdigest(), self.game_id, name, now, now+180, now+43200, now))
                return dict(id=sid, token=token, expires_at=now+180, absolute_expiry=now+43200)
            server = self.auth(db, authorization, now)
            match = re.fullmatch(r"/v1/server-sessions/([^/]+)/(heartbeat|close)", path)
            if method == "POST" and match:
                if match[1] != server["id"]:
                    fail(403, "session_binding", "Wrong server session")
                if match[2] == "close":
                    db.execute("UPDATE server_sessions SET revoked_at=? WHERE id=?", (now, server["id"]))
                    db.execute("DELETE FROM edit_leases WHERE server_id=?", (server["id"],))
                    return {"closed": True}
                count, name = data.get("player_count"), data.get("display_name", server["display_name"])
                if type(count) is not int or not 0 <= count <= 1000 or not isinstance(name, str) or len(name) > 80:
                    fail(400, "invalid_heartbeat", "Invalid heartbeat")
                expiry = min(now+180, server["absolute_expiry"])
                db.execute("UPDATE server_sessions SET expires_at=?,heartbeat_at=?,player_count=?,display_name=? WHERE id=?", (expiry, now, count, name, server["id"]))
                db.execute("UPDATE player_sessions SET expires_at=? WHERE server_id=? AND closed_at IS NULL AND expires_at>?", (expiry, server["id"], now))
                return {"expires_at": expiry}
            if method == "POST" and path == "/v1/player-sessions":
                player_id = data.get("player_id")
                if type(player_id) is not int or player_id < 0:
                    fail(400, "invalid_player", "Invalid connected player")
                self.bootstrap(db, player_id, now)
                existing = db.execute("SELECT * FROM player_sessions WHERE server_id=? AND player_id=?", (server["id"], player_id)).fetchone()
                if existing and existing["closed_at"] is None and existing["expires_at"] > now:
                    return dict(id=existing["id"], expires_at=existing["expires_at"])
                if existing:
                    db.execute("DELETE FROM edit_leases WHERE presence_id=?", (existing["id"],))
                    db.execute("DELETE FROM player_sessions WHERE id=?", (existing["id"],))
                pid = secrets.token_hex(16)
                db.execute("INSERT INTO player_sessions VALUES(?,?,?,?,NULL)", (pid, player_id, server["id"], server["expires_at"]))
                return dict(id=pid, expires_at=server["expires_at"])
            match = re.fullmatch(r"/v1/player-sessions/([^/]+)", path)
            if method == "DELETE" and match:
                presence = self.presence(db, server, match[1], now)
                db.execute("UPDATE player_sessions SET closed_at=? WHERE id=?", (now, presence["id"]))
                db.execute("DELETE FROM edit_leases WHERE presence_id=?", (presence["id"],))
                return {"closed": True}
            presence_id = data.get("presence_id") if method != "GET" else query.get("presence_id", [None])[0]
            presence = self.presence(db, server, presence_id, now)
            from publication import dispatch as publication_dispatch
            published = publication_dispatch(self, db, method, path, query, data, server, presence, now)
            if published is not None:
                return published
            match = re.fullmatch(r"/v1/players/(\d+)/bootstrap", path)
            if method == "GET" and match:
                if int(match[1]) != presence["player_id"]:
                    fail(403, "player_owner", "This player is unavailable")
                return self.bootstrap(db, presence["player_id"], now, include_drafts=query.get("include_drafts", [None])[0] == "1")
            match = re.fullmatch(r"/v1/slots/([^/]+)/(edit-lease|draft)", path)
            if not match:
                fail(404, "not_found", "Endpoint unavailable")
            slot = self.slot(db, match[1], presence)
            lease = db.execute("SELECT * FROM edit_leases WHERE slot_id=?", (slot["id"],)).fetchone()
            if match[2] == "edit-lease":
                if method == "POST":
                    if lease and lease["expires_at"] > now and (lease["server_id"] != server["id"] or lease["presence_id"] != presence["id"]):
                        live = db.execute("SELECT s.expires_at,s.revoked_at,p.closed_at,p.expires_at AS presence_expiry FROM server_sessions s JOIN player_sessions p ON p.server_id=s.id WHERE s.id=? AND p.id=?", (lease["server_id"], lease["presence_id"])).fetchone()
                        if live and live["revoked_at"] is None and live["closed_at"] is None and min(live["expires_at"], live["presence_expiry"]) > now:
                            fail(409, "lease_held", "This slot is being edited in another server")
                    lid = lease["id"] if lease and lease["server_id"] == server["id"] and lease["presence_id"] == presence["id"] and lease["expires_at"] > now else secrets.token_hex(16)
                    expiry = min(now+180, server["absolute_expiry"])
                    db.execute("INSERT INTO edit_leases VALUES(?,?,?,?,?) ON CONFLICT(slot_id) DO UPDATE SET id=excluded.id,server_id=excluded.server_id,presence_id=excluded.presence_id,expires_at=excluded.expires_at", (slot["id"], lid, server["id"], presence["id"], expiry))
                    revision = db.execute("SELECT revision FROM drafts WHERE slot_id=?", (slot["id"],)).fetchone()[0]
                    return dict(id=lid, expires_at=expiry, draft_revision=revision)
                if method == "DELETE":
                    if lease and (lease["server_id"] != server["id"] or lease["presence_id"] != presence["id"] or lease["id"] != data.get("lease_id")):
                        fail(403, "lease_binding", "Wrong edit lease")
                    db.execute("DELETE FROM edit_leases WHERE slot_id=?", (slot["id"],))
                    return {"released": True}
            draft = db.execute("SELECT * FROM drafts WHERE slot_id=?", (slot["id"],)).fetchone()
            if match[2] == "draft" and method == "GET":
                return self.draft_payload(db, slot, draft)
            if match[2] != "draft" or method != "PUT":
                fail(405, "method_unavailable", "Method unavailable")
            request_id = data.get("request_id")
            if not identifier(request_id):
                fail(400, "request_id", "A bounded request ID is required")
            digest = hashlib.sha256((path+canonical(data)).encode()).hexdigest()
            cached = db.execute("SELECT * FROM idempotency_records WHERE server_id=? AND request_id=?", (server["id"], request_id)).fetchone()
            if cached:
                if cached["request_digest"] != digest:
                    fail(409, "idempotency_mismatch", "Request ID was already used for different data")
                return json.loads(cached["response_json"])
            if not lease or lease["expires_at"] <= now or lease["server_id"] != server["id"] or lease["presence_id"] != presence["id"] or lease["id"] != data.get("lease_id"):
                fail(409, "lease_expired", "Acquire an active edit lease before saving")
            if type(data.get("expected_revision")) is not int or data["expected_revision"] != draft["revision"]:
                fail(409, "revision_conflict", "A newer draft exists; reload before saving")
            stock = dict(db.execute("SELECT piece_type,quantity FROM inventory WHERE player_id=?", (presence["player_id"],)).fetchall())
            try:
                layout = validate(normalize_empty_tables(data.get("layout")), stock)
                encoded = canonical(layout)
            except (ValueError, TypeError, OverflowError):
                fail(400, "invalid_layout", "Layout violates geometry, inventory, or schema rules")
            if len(encoded.encode()) > 131072:
                fail(413, "layout_too_large", "Draft snapshot is too large")
            result = dict(revision=draft["revision"]+1, content_hash=content_hash(layout), updated_at=now)
            db.execute("UPDATE drafts SET layout_json=?,revision=?,content_hash=?,updated_at=? WHERE slot_id=?", (encoded, result["revision"], result["content_hash"], now, slot["id"]))
            db.execute("INSERT INTO idempotency_records VALUES(?,?,?,?,?)", (server["id"], request_id, digest, canonical(result), now))
            return result
