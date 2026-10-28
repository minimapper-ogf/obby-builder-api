"""Online backup and bounded operational history retention."""
import argparse
from pathlib import Path
import sqlite3
import time


def backup(database, destination):
    if not Path(database).is_file():
        raise ValueError("Source database does not exist")
    if Path(database).resolve() == Path(destination).resolve() or Path(destination).exists():
        raise ValueError("Use a new backup destination")
    source = sqlite3.connect(database)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Backup integrity check failed")
    finally:
        target.close()
        source.close()


def maintain(database):
    if not Path(database).is_file():
        raise ValueError("Database does not exist")
    cutoff = time.time()-7*86400
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA foreign_keys=ON")
        if db.execute("PRAGMA user_version").fetchone()[0] >= 2:
            db.execute("DELETE FROM discovery_cursors WHERE expires_at<?", (time.time(),))
        db.execute("DELETE FROM edit_leases WHERE expires_at<?", (time.time(),))
        db.execute("DELETE FROM idempotency_records WHERE created_at<?", (cutoff,))
        db.execute("DELETE FROM player_sessions WHERE server_id IN (SELECT id FROM server_sessions WHERE absolute_expiry<?)", (cutoff,))
        db.execute("DELETE FROM server_sessions WHERE absolute_expiry<?", (cutoff,))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["backup", "maintain"])
    parser.add_argument("database")
    parser.add_argument("destination", nargs="?")
    args = parser.parse_args()
    if args.action == "backup":
        if not args.destination:
            parser.error("Backup requires a new destination path")
        backup(args.database, args.destination)
    else:
        maintain(args.database)
