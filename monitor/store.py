import contextlib
import json
import os
import sqlite3
from pathlib import Path


@contextlib.contextmanager
def process_lock(path):
    """OS releases the advisory lock even after a crash."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS alerts (
                event_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                campaign_id TEXT, updated_at TEXT NOT NULL);
        """)
        self.db.commit()
        self._batch = False

    @contextlib.contextmanager
    def transaction(self):
        if self._batch:
            raise RuntimeError("Nested state transaction")
        self.db.execute("BEGIN IMMEDIATE")
        self._batch = True
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        finally:
            self._batch = False

    def _commit(self):
        if not self._batch:
            self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))
        self._commit()

    def unseen(self, pid):
        return self.db.execute("SELECT 1 FROM seen WHERE id=?", (pid,)).fetchone() is None

    def mark_seen(self, ids):
        self.db.executemany("INSERT OR IGNORE INTO seen VALUES (?)", [(i,) for i in ids])
        self._commit()

    def claim(self, event_id, status, now):
        cursor = self.db.execute("INSERT OR IGNORE INTO alerts VALUES (?,?,NULL,?)", (event_id, status, now))
        self._commit()
        return cursor.rowcount == 1

    def alert(self, event_id):
        row = self.db.execute("SELECT status,campaign_id FROM alerts WHERE event_id=?", (event_id,)).fetchone()
        return {"status": row[0], "campaignId": row[1]} if row else None

    def set_alert(self, event_id, status, campaign_id, now):
        self.db.execute("UPDATE alerts SET status=?,campaign_id=?,updated_at=? WHERE event_id=?",
                        (status, campaign_id, now, event_id))
        self._commit()

    def close(self):
        self.db.close()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
