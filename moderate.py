"""Local operator controls. No admin capability or credential is exposed to game clients."""
import argparse
import json
from pathlib import Path
import sqlite3
import time


def moderate(database, action, target=None, reason=""):
    if not Path(database).is_file():
        raise ValueError("Database does not exist")
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        if db.execute("PRAGMA user_version").fetchone()[0] != 2:
            raise ValueError("Start the updated API once to migrate the database first")
        if action == "reports":
            return [dict(r) for r in db.execute("SELECT * FROM reports WHERE status='open' ORDER BY created_at LIMIT 100")]
        if action == "inspect":
            course = db.execute("SELECT * FROM courses WHERE id=?", (target,)).fetchone()
            if not course:
                raise ValueError("Course not found")
            return dict(course=dict(course), versions=[dict(r) for r in db.execute("SELECT * FROM course_versions WHERE course_id=? ORDER BY version_number DESC LIMIT 20", (target,))])
        if not 1 <= len(reason.strip()) <= 400:
            raise ValueError("Provide a short operator reason (1–400 characters)")
        if action in ("hide", "unhide", "feature", "unfeature"):
            column = "hidden" if action in ("hide", "unhide") else "featured"
            changed = db.execute(f"UPDATE courses SET {column}=? WHERE id=?", (int(action in ("hide", "feature")), target)).rowcount
        elif action == "suspend":
            if not db.execute("SELECT 1 FROM players WHERE id=?", (target,)).fetchone():
                raise ValueError("Player not found")
            db.execute("INSERT INTO publishing_suspensions VALUES(?,?) ON CONFLICT(player_id) DO UPDATE SET reason=excluded.reason", (target, reason))
            changed = 1
        elif action == "unsuspend":
            changed = db.execute("DELETE FROM publishing_suspensions WHERE player_id=?", (target,)).rowcount
        elif action == "resolve":
            changed = db.execute("UPDATE reports SET status='resolved',resolution=? WHERE id=?", (reason, target)).rowcount
        else:
            raise ValueError("Unknown moderation action")
        if not changed:
            raise ValueError("Target not found")
        db.execute("INSERT INTO moderation_actions(action,target,reason,created_at) VALUES(?,?,?,?)", (action, target, reason, time.time()))
        return dict(action=action, target=target, reason=reason)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database")
    parser.add_argument("action", choices=["reports", "inspect", "hide", "unhide", "feature", "unfeature", "suspend", "unsuspend", "resolve"])
    parser.add_argument("target", nargs="?")
    parser.add_argument("--reason", default="")
    args = parser.parse_args()
    print(json.dumps(moderate(args.database, args.action, args.target, args.reason), indent=2))
