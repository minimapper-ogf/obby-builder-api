"""Publication and discovery. Called inside the authenticated service transaction.

Trusted game servers observe gameplay and filter text; clients never supply proof
directly. Currency, purchases and pass entitlement verification are separate.
"""
import hashlib
import json
import math
import re
import secrets

from layouts import canonical, validate
from service import fail, identifier


ELIGIBLE = "c.visibility='public' AND c.hidden=0 AND c.deleted_at IS NULL AND c.current_published_version IS NOT NULL"


def public_course(db, course_id):
    course = db.execute(f"SELECT c.* FROM courses c WHERE c.id=? AND {ELIGIBLE}", (course_id,)).fetchone()
    if not course:
        fail(404, "course_unavailable", "This course is unavailable")
    return course


def require_lease(db, slot_id, data, server, presence, now):
    lease = db.execute("SELECT * FROM edit_leases WHERE slot_id=?", (slot_id,)).fetchone()
    if not lease or lease["expires_at"] <= now or lease["server_id"] != server["id"] or lease["presence_id"] != presence["id"] or lease["id"] != data.get("lease_id"):
        fail(409, "lease_expired", "Open this slot for editing first")


def metadata(data):
    title, description = data.get("title"), data.get("description", "")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 60 or not isinstance(description, str) or len(description) > 400:
        fail(400, "metadata_invalid", "Use a title of 1–60 characters and description up to 400 characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in title+description) or data.get("text_filtered") is not True:
        fail(400, "metadata_unfiltered", "Public text must pass the platform filter")
    return title.strip(), description.strip()


def proof(data, layout, started_at, now):
    elapsed, deaths = data.get("elapsed"), data.get("deaths")
    checkpoints = data.get("checkpoints")
    if checkpoints == {}:  # Luau serialises an empty table as an object.
        checkpoints = []
    if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or not 0.1 <= elapsed <= 3600 or elapsed > now-started_at+2:
        fail(400, "run_time", "Run duration is invalid")
    if type(deaths) is not int or not 0 <= deaths <= 10000 or checkpoints != layout["checkpoint_order"]:
        fail(400, "run_checkpoints", "All checkpoints must be reached in order")
    return canonical(dict(elapsed=elapsed, deaths=deaths, checkpoints=checkpoints))


def card(db, course, player_id):
    result = {key: course[key] for key in ("id", "owner_id", "title", "description", "current_published_version", "featured")}
    result["likes"] = db.execute("SELECT COUNT(*) FROM votes WHERE course_id=?", (course["id"],)).fetchone()[0]
    result["visits"] = db.execute("SELECT COUNT(*) FROM visit_events WHERE course_id=?", (course["id"],)).fetchone()[0]
    for table, key in (("votes", "liked"), ("favourites", "favourited")):
        result[key] = bool(db.execute(f"SELECT 1 FROM {table} WHERE course_id=? AND player_id=?", (course["id"], player_id)).fetchone())
    return result


def dispatch(service, db, method, path, query, data, server, presence, now):
    """Return None when the existing draft router should handle the request."""
    supported = bool(re.fullmatch(r"/v1/(?:slots/[^/]+/(?:verification-runs|publish)|verification-runs/[^/]+/finish|courses(?:/[^/]+(?:/(?:unpublish|vote|favourite|reports))?)?|course-versions/[^/]+|runs(?:/[^/]+/finish)?)", path))
    if not supported:
        return None
    request_id = data.get("request_id")
    digest = None
    if method != "GET":
        if not identifier(request_id):
            fail(400, "request_id", "A bounded request ID is required")
        digest = hashlib.sha256((method+path+canonical(data)).encode()).hexdigest()
        cached = db.execute("SELECT * FROM idempotency_records WHERE server_id=? AND request_id=?", (server["id"], request_id)).fetchone()
        if cached:
            if cached["request_digest"] != digest:
                fail(409, "idempotency_mismatch", "Request ID was used for different data")
            return json.loads(cached["response_json"])
    result = route(service, db, method, path, query, data, server, presence, now)
    if digest:
        db.execute("INSERT INTO idempotency_records VALUES(?,?,?,?,?)", (server["id"], request_id, digest, canonical(result), now))
    return result


def route(service, db, method, path, query, data, server, presence, now):
    player = presence["player_id"]
    match = re.fullmatch(r"/v1/slots/([^/]+)/(verification-runs|publish)", path)
    if method == "POST" and match:
        slot = service.slot(db, match[1], presence)
        course = db.execute("SELECT * FROM courses WHERE id=?", (slot["course_id"],)).fetchone()
        if course["deleted_at"] is not None or course["hidden"] or db.execute("SELECT 1 FROM publishing_suspensions WHERE player_id=?", (player,)).fetchone():
            fail(403, "publishing_unavailable", "Publishing is unavailable for this course or account")
        require_lease(db, slot["id"], data, server, presence, now)
        draft = db.execute("SELECT * FROM drafts WHERE slot_id=?", (slot["id"],)).fetchone()
        if type(data.get("expected_revision")) is not int or data["expected_revision"] != draft["revision"]:
            fail(409, "revision_conflict", "Save and verify the latest draft first")
        stock = dict(db.execute("SELECT piece_type,quantity FROM inventory WHERE player_id=?", (player,)))
        try:
            layout = validate(json.loads(draft["layout_json"]), stock)
        except (ValueError, TypeError):
            fail(400, "invalid_layout", "The draft cannot be published")
        if sum(p["type"] == "finish" for p in layout["parts"]) != 1:
            fail(400, "finish_required", "Add exactly one finish before verification")
        if match[2] == "verification-runs":
            if db.execute("SELECT COUNT(*) FROM verification_runs WHERE player_id=? AND created_at>?", (player, now-60)).fetchone()[0] >= 10:
                fail(429, "verification_limited", "Please wait before verifying again")
            rid = secrets.token_hex(16)
            db.execute("INSERT INTO verification_runs(id,slot_id,player_id,server_id,presence_id,revision,content_hash,rules_version,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (rid, slot["id"], player, server["id"], presence["id"], draft["revision"], draft["content_hash"], layout["rules_version"], now, now+3600))
            return dict(id=rid, revision=draft["revision"], content_hash=draft["content_hash"], expires_at=now+3600)
        title, description = metadata(data)
        verified = db.execute("SELECT * FROM verification_runs WHERE id=?", (data.get("verification_id"),)).fetchone()
        if not verified or verified["slot_id"] != slot["id"] or verified["player_id"] != player or verified["finished_at"] is None or verified["revision"] != draft["revision"] or verified["content_hash"] != draft["content_hash"] or verified["rules_version"] != layout["rules_version"]:
            fail(409, "verification_required", "Complete a verification run of this saved draft first")
        previous = db.execute("SELECT * FROM course_versions WHERE id=?", (course["current_published_version"],)).fetchone()
        if previous and previous["content_hash"] == draft["content_hash"] and previous["title"] == title and previous["description"] == description:
            version_id, number = previous["id"], previous["version_number"]
        else:
            version_id = secrets.token_hex(16)
            number = db.execute("SELECT COALESCE(MAX(version_number),0)+1 FROM course_versions WHERE course_id=?", (course["id"],)).fetchone()[0]
            db.execute("INSERT INTO course_versions VALUES(?,?,?,?,?,?,?,?,?)", (version_id, course["id"], number, draft["layout_json"], draft["content_hash"], verified["id"], title, description, now))
        db.execute("UPDATE courses SET title=?,description=?,visibility='public',current_published_version=? WHERE id=?", (title, description, version_id, course["id"]))
        return dict(course_id=course["id"], version_id=version_id, version_number=number)
    match = re.fullmatch(r"/v1/verification-runs/([^/]+)/finish", path)
    if method == "POST" and match:
        run = db.execute("SELECT * FROM verification_runs WHERE id=?", (match[1],)).fetchone()
        if not run or run["server_id"] != server["id"] or run["presence_id"] != presence["id"]:
            fail(403, "verification_binding", "Verification belongs to another player or server")
        draft = db.execute("SELECT * FROM drafts WHERE slot_id=?", (run["slot_id"],)).fetchone()
        if run["revision"] != draft["revision"] or run["content_hash"] != draft["content_hash"]:
            fail(409, "verification_stale", "Draft changed; start a new verification")
        encoded = proof(data, json.loads(draft["layout_json"]), run["created_at"], now)
        if run["finished_at"] is not None:
            if run["proof_json"] != encoded:
                fail(409, "proof_mismatch", "Verification was already finished differently")
        elif run["expires_at"] <= now:
            fail(409, "verification_expired", "Start a new verification run")
        else:
            db.execute("UPDATE verification_runs SET finished_at=?,elapsed=?,proof_json=? WHERE id=?", (now, data["elapsed"], encoded, run["id"]))
        return dict(verified=True, id=run["id"], revision=run["revision"])
    if method == "GET" and path == "/v1/courses":
        return discover(db, query, server, presence, now)
    match = re.fullmatch(r"/v1/courses/([^/]+)", path)
    if method == "GET" and match:
        return card(db, public_course(db, match[1]), player)
    match = re.fullmatch(r"/v1/course-versions/([^/]+)", path)
    if method == "GET" and match:
        version = db.execute("SELECT * FROM course_versions WHERE id=?", (match[1],)).fetchone()
        if not version:
            fail(404, "course_unavailable", "This course is unavailable")
        public_course(db, version["course_id"])
        result = dict(version)
        result["layout"] = json.loads(result.pop("layout_json"))
        return result
    match = re.fullmatch(r"/v1/courses/([^/]+)/(unpublish|vote|favourite|reports)", path)
    if match:
        if method == "POST" and match[2] == "unpublish":
            course = db.execute("SELECT * FROM courses WHERE id=? AND owner_id=? AND deleted_at IS NULL", (match[1], player)).fetchone()
            if not course:
                fail(403, "course_owner", "Only the owner can unpublish this course")
            db.execute("UPDATE courses SET visibility='draft' WHERE id=?", (course["id"],))
            return dict(unpublished=True)
        course = public_course(db, match[1])
        if method == "PUT" and match[2] in ("vote", "favourite"):
            if type(data.get("value")) is not bool:
                fail(400, "invalid_vote", "Choose true or false")
            table = "votes" if match[2] == "vote" else "favourites"
            if table == "votes" and (course["owner_id"] == player or not db.execute("SELECT 1 FROM runs WHERE course_id=? AND player_id=?", (course["id"], player)).fetchone()):
                fail(403, "vote_ineligible", "Start this course before voting; self-votes are excluded")
            if data["value"]:
                db.execute(f"INSERT OR IGNORE INTO {table} VALUES(?,?,?)", (course["id"], player, now))
            else:
                db.execute(f"DELETE FROM {table} WHERE course_id=? AND player_id=?", (course["id"], player))
            return card(db, course, player)
        if method == "POST" and match[2] == "reports":
            category = data.get("category")
            if category not in ("inappropriate", "unplayable", "other"):
                fail(400, "report_category", "Choose a report category")
            if db.execute("SELECT COUNT(*) FROM reports WHERE player_id=? AND created_at>?", (player, now-3600)).fetchone()[0] >= 5:
                fail(429, "report_limited", "Report limit reached; try later")
            version_id = data.get("version_id", course["current_published_version"])
            if not db.execute("SELECT 1 FROM course_versions WHERE id=? AND course_id=?", (version_id, course["id"])).fetchone():
                fail(400, "report_version", "Reported version does not belong to this course")
            rid = secrets.token_hex(16)
            db.execute("INSERT INTO reports(id,course_id,version_id,player_id,category,created_at) VALUES(?,?,?,?,?,?)", (rid, course["id"], version_id, player, category, now))
            return dict(reported=True)
    if method == "POST" and path == "/v1/runs":
        version = db.execute("SELECT * FROM course_versions WHERE id=?", (data.get("version_id"),)).fetchone()
        if not version:
            fail(404, "course_unavailable", "This course is unavailable")
        public_course(db, version["course_id"])
        delay = data.get("elapsed_before_registration", 0)
        if type(delay) not in (int, float) or not math.isfinite(delay) or not 0 <= delay <= 120:
            fail(400, "run_delay", "Run registration was delayed too long; restart the course")
        started_at = now-delay
        rid = secrets.token_hex(16)
        db.execute("INSERT INTO runs(id,course_id,version_id,player_id,server_id,presence_id,started_at) VALUES(?,?,?,?,?,?,?)", (rid, version["course_id"], version["id"], player, server["id"], presence["id"], started_at))
        db.execute("INSERT OR IGNORE INTO visit_events VALUES(?,?,?,?)", (version["course_id"], player, int(started_at//86400), started_at))
        return dict(id=rid, version_id=version["id"], coins=0)
    match = re.fullmatch(r"/v1/runs/([^/]+)/finish", path)
    if method == "POST" and match:
        run = db.execute("SELECT * FROM runs WHERE id=?", (match[1],)).fetchone()
        if not run or run["server_id"] != server["id"] or run["presence_id"] != presence["id"]:
            fail(403, "run_binding", "Run belongs to another player or server")
        version = db.execute("SELECT * FROM course_versions WHERE id=?", (run["version_id"],)).fetchone()
        encoded = proof(data, json.loads(version["layout_json"]), run["started_at"], now)
        if run["finished_at"] is not None and run["proof_json"] != encoded:
            fail(409, "proof_mismatch", "Run was already finished differently")
        if run["finished_at"] is None:
            db.execute("UPDATE runs SET finished_at=?,elapsed=?,deaths=?,proof_json=? WHERE id=?", (now, data["elapsed"], data["deaths"], encoded, run["id"]))
        return dict(recorded=True, coins=0, version_id=run["version_id"])
    fail(405, "method_unavailable", "Method unavailable")


def discover(db, query, server, presence, now):
    cursor = query.get("cursor", [""])[0]
    if cursor:
        row = db.execute("SELECT * FROM discovery_cursors WHERE id=? AND server_id=? AND presence_id=? AND expires_at>?", (cursor, server["id"], presence["id"], now)).fetchone()
        if not row:
            fail(400, "cursor_expired", "Refresh the course list")
        ids = json.loads(row["remaining_json"])
    else:
        mode, search = query.get("sort", ["new"])[0], query.get("search", [""])[0]
        if len(search) > 80:
            fail(400, "search_invalid", "Search is too long")
        orders = {"new": "v.published_at DESC,c.id", "featured": "c.featured DESC,v.published_at DESC,c.id", "liked": "likes DESC,c.id", "visited": "visits DESC,c.id", "popular": "weekly DESC,c.id", "random": "(recent IS NOT NULL),RANDOM()", "favourites": "v.published_at DESC,c.id", "recent": "recent DESC,c.id"}
        if mode not in orders:
            fail(400, "sort_invalid", "Unknown course sort")
        args = [presence["player_id"], now-7*86400]
        where = ELIGIBLE
        if search:
            literal = search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where += " AND (lower(c.title) LIKE ? ESCAPE '\\' OR upper(c.id)=?)"
            args += ["%"+literal+"%", search.upper()]
        if mode == "favourites":
            where += " AND EXISTS(SELECT 1 FROM favourites f WHERE f.course_id=c.id AND f.player_id=?)"
            args.append(presence["player_id"])
        if mode == "recent":
            where += " AND recent IS NOT NULL"
        if mode == "featured":
            where += " AND c.featured=1"
        ids = [row["id"] for row in db.execute(f"SELECT c.id,(SELECT MAX(started_at) FROM runs WHERE course_id=c.id AND player_id=?) recent,(SELECT COUNT(*) FROM votes WHERE course_id=c.id) likes,(SELECT COUNT(*) FROM visit_events WHERE course_id=c.id) visits,(SELECT COUNT(*) FROM visit_events WHERE course_id=c.id AND created_at>?) weekly FROM courses c JOIN course_versions v ON v.id=c.current_published_version WHERE {where} ORDER BY {orders[mode]} LIMIT ?", args+[20 if mode == "random" else 1000])]
    items = []
    while ids and len(items) < 20:
        course_id = ids.pop(0)
        course = db.execute(f"SELECT c.* FROM courses c WHERE c.id=? AND {ELIGIBLE}", (course_id,)).fetchone()
        if course:
            items.append(card(db, course, presence["player_id"]))
    next_cursor = ""
    if ids:
        db.execute("DELETE FROM discovery_cursors WHERE expires_at<=?", (now,))
        if db.execute("SELECT COUNT(*) FROM discovery_cursors WHERE presence_id=?", (presence["id"],)).fetchone()[0] >= 100:
            fail(429, "browse_limited", "Please wait before browsing more pages")
        next_cursor = secrets.token_hex(16)
        db.execute("INSERT INTO discovery_cursors VALUES(?,?,?,?,?)", (next_cursor, server["id"], presence["id"], canonical(ids), now+600))
    return dict(items=items, next_cursor=next_cursor)
