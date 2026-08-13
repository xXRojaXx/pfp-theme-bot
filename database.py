from __future__ import annotations

import random
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS group_rounds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    closed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    round_id INTEGER NOT NULL,
                    theme TEXT NOT NULL,
                    suggested_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(round_id) REFERENCES group_rounds(id) ON DELETE CASCADE
                );

                CREATE UNIQUE INDEX IF NOT EXISTS uq_suggestion_round_theme
                ON suggestions(round_id, theme COLLATE NOCASE);

                CREATE TABLE IF NOT EXISTS votes (
                    round_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    suggestion_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(round_id, user_id),
                    FOREIGN KEY(round_id) REFERENCES group_rounds(id) ON DELETE CASCADE,
                    FOREIGN KEY(suggestion_id) REFERENCES suggestions(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS group_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    round_id INTEGER NOT NULL,
                    theme TEXT NOT NULL,
                    votes INTEGER NOT NULL DEFAULT 0,
                    closed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS random_rounds (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    closed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS random_assignments (
                    round_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    theme TEXT NOT NULL,
                    assigned_at TEXT NOT NULL,
                    PRIMARY KEY(round_id, user_id),
                    UNIQUE(round_id, theme),
                    FOREIGN KEY(round_id) REFERENCES random_rounds(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS used_themes (
                    theme TEXT PRIMARY KEY COLLATE NOCASE,
                    used_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    user_id INTEGER,
                    round_id INTEGER
                );

                CREATE TABLE IF NOT EXISTS custom_themes (
                    theme TEXT PRIMARY KEY COLLATE NOCASE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS disabled_themes (
                    theme TEXT PRIMARY KEY COLLATE NOCASE,
                    disabled_at TEXT NOT NULL
                );
                """
            )

    # ---------- Group voting ----------

    def active_group_round(self):
        return self._conn.execute(
            "SELECT * FROM group_rounds WHERE status != 'closed' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def create_group_round(self) -> int:
        with self._lock, self._conn:
            active = self.active_group_round()
            if active:
                return int(active["id"])
            cur = self._conn.execute(
                "INSERT INTO group_rounds(status, created_at) VALUES('open', ?)",
                (utc_now(),),
            )
            return int(cur.lastrowid)

    def set_group_status(self, round_id: int, status: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE group_rounds SET status=? WHERE id=?",
                (status, round_id),
            )

    def add_suggestion(self, round_id: int, theme: str, user_id: int) -> bool:
        theme = " ".join(theme.split()).strip()
        if not theme:
            return False
        with self._lock, self._conn:
            if self.is_theme_used(theme):
                return False
            try:
                self._conn.execute(
                    "INSERT INTO suggestions(round_id, theme, suggested_by, created_at) VALUES(?,?,?,?)",
                    (round_id, theme, user_id, utc_now()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def get_suggestions(self, round_id: int):
        return self._conn.execute(
            """
            SELECT s.id, s.theme, s.suggested_by,
                   COUNT(v.user_id) AS vote_count
            FROM suggestions s
            LEFT JOIN votes v ON v.suggestion_id = s.id AND v.round_id = s.round_id
            WHERE s.round_id=?
            GROUP BY s.id
            ORDER BY s.id ASC
            """,
            (round_id,),
        ).fetchall()

    def cast_vote(self, round_id: int, user_id: int, suggestion_id: int) -> bool:
        row = self._conn.execute(
            "SELECT id FROM suggestions WHERE id=? AND round_id=?",
            (suggestion_id, round_id),
        ).fetchone()
        if not row:
            return False
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO votes(round_id, user_id, suggestion_id, updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(round_id, user_id)
                DO UPDATE SET suggestion_id=excluded.suggestion_id, updated_at=excluded.updated_at
                """,
                (round_id, user_id, suggestion_id, utc_now()),
            )
        return True

    def get_user_vote(self, round_id: int, user_id: int):
        return self._conn.execute(
            """
            SELECT s.theme
            FROM votes v
            JOIN suggestions s ON s.id=v.suggestion_id
            WHERE v.round_id=? AND v.user_id=?
            """,
            (round_id, user_id),
        ).fetchone()

    def close_group_round(self, round_id: int):
        with self._lock, self._conn:
            rows = self.get_suggestions(round_id)
            if not rows:
                return None

            max_votes = max(int(r["vote_count"]) for r in rows)
            finalists = [r for r in rows if int(r["vote_count"]) == max_votes]
            winner = random.choice(finalists)
            now = utc_now()

            self._conn.execute(
                "UPDATE group_rounds SET status='closed', closed_at=? WHERE id=?",
                (now, round_id),
            )
            self._conn.execute(
                "INSERT INTO group_history(round_id, theme, votes, closed_at) VALUES(?,?,?,?)",
                (round_id, winner["theme"], max_votes, now),
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO used_themes(theme, used_at, source, user_id, round_id)
                VALUES(?, ?, 'group_vote', NULL, ?)
                """,
                (winner["theme"], now, round_id),
            )
            return {
                "theme": winner["theme"],
                "votes": max_votes,
                "tie": len(finalists) > 1,
                "finalists": [r["theme"] for r in finalists],
            }

    def group_history(self, limit: int = 20):
        return self._conn.execute(
            "SELECT * FROM group_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()


    def pick_random_suggestion(self, round_id: int, built_in_themes: Iterable[str]):
        """Pick one random theme not already suggested this round and not used as a past winner."""
        with self._lock:
            current = {
                r["theme"].casefold().strip()
                for r in self._conn.execute(
                    "SELECT theme FROM suggestions WHERE round_id=?",
                    (round_id,),
                ).fetchall()
            }
            custom = [
                r["theme"] for r in self._conn.execute(
                    "SELECT theme FROM custom_themes WHERE enabled=1"
                ).fetchall()
            ]
            disabled = {
                r["theme"].casefold().strip()
                for r in self._conn.execute(
                    "SELECT theme FROM disabled_themes"
                ).fetchall()
            }
            used_winners = {
                r["theme"].casefold().strip()
                for r in self._conn.execute(
                    "SELECT theme FROM group_history"
                ).fetchall()
            }

            pool = []
            seen = set()
            for theme in list(built_in_themes) + custom:
                key = theme.casefold().strip()
                if (
                    not key
                    or key in seen
                    or key in current
                    or key in disabled
                    or key in used_winners
                ):
                    continue
                seen.add(key)
                pool.append(theme.strip())

            if not pool:
                return None

            return random.SystemRandom().choice(pool)

    # ---------- Random assignment ----------

    def active_random_round(self):
        return self._conn.execute(
            "SELECT * FROM random_rounds WHERE active=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def ensure_random_round(self) -> int:
        with self._lock, self._conn:
            row = self.active_random_round()
            if row:
                return int(row["id"])
            cur = self._conn.execute(
                "INSERT INTO random_rounds(active, created_at) VALUES(1,?)",
                (utc_now(),),
            )
            return int(cur.lastrowid)

    def reset_random_round(self) -> int:
        with self._lock, self._conn:
            now = utc_now()
            self._conn.execute(
                "UPDATE random_rounds SET active=0, closed_at=? WHERE active=1",
                (now,),
            )
            cur = self._conn.execute(
                "INSERT INTO random_rounds(active, created_at) VALUES(1,?)",
                (now,),
            )
            return int(cur.lastrowid)

    def get_random_assignment(self, round_id: int, user_id: int):
        return self._conn.execute(
            "SELECT * FROM random_assignments WHERE round_id=? AND user_id=?",
            (round_id, user_id),
        ).fetchone()

    def assign_random_theme(self, user_id: int, built_in_themes: Iterable[str]):
        """Atomically return an existing assignment or reserve one new unique theme."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM random_rounds WHERE active=1 ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row:
                    round_id = int(row["id"])
                else:
                    cur = self._conn.execute(
                        "INSERT INTO random_rounds(active, created_at) VALUES(1,?)",
                        (utc_now(),),
                    )
                    round_id = int(cur.lastrowid)

                existing = self.get_random_assignment(round_id, user_id)
                if existing:
                    self._conn.commit()
                    return dict(existing), False

                custom = [
                    r["theme"] for r in self._conn.execute(
                        "SELECT theme FROM custom_themes WHERE enabled=1"
                    ).fetchall()
                ]
                disabled = {
                    r["theme"].casefold() for r in self._conn.execute(
                        "SELECT theme FROM disabled_themes"
                    ).fetchall()
                }
                used = {
                    r["theme"].casefold() for r in self._conn.execute(
                        "SELECT theme FROM used_themes"
                    ).fetchall()
                }

                pool = []
                seen = set()
                for theme in list(built_in_themes) + custom:
                    key = theme.casefold().strip()
                    if not key or key in seen or key in disabled or key in used:
                        continue
                    seen.add(key)
                    pool.append(theme.strip())

                if not pool:
                    self._conn.rollback()
                    return None, False

                theme = random.SystemRandom().choice(pool)
                now = utc_now()
                self._conn.execute(
                    "INSERT INTO random_assignments(round_id, user_id, theme, assigned_at) VALUES(?,?,?,?)",
                    (round_id, user_id, theme, now),
                )
                self._conn.execute(
                    "INSERT INTO used_themes(theme, used_at, source, user_id, round_id) VALUES(?,?,?,?,?)",
                    (theme, now, "random", user_id, round_id),
                )
                self._conn.commit()
                return {
                    "round_id": round_id,
                    "user_id": user_id,
                    "theme": theme,
                    "assigned_at": now,
                }, True
            except Exception:
                self._conn.rollback()
                raise

    def reroll_random_theme(self, user_id: int, built_in_themes: Iterable[str]):
        """Remove only the current assignment; the old theme remains in used history."""
        with self._lock, self._conn:
            round_id = self.ensure_random_round()
            self._conn.execute(
                "DELETE FROM random_assignments WHERE round_id=? AND user_id=?",
                (round_id, user_id),
            )
        return self.assign_random_theme(user_id, built_in_themes)

    def random_assignments(self):
        row = self.active_random_round()
        if not row:
            return []
        return self._conn.execute(
            "SELECT * FROM random_assignments WHERE round_id=? ORDER BY assigned_at ASC",
            (int(row["id"]),),
        ).fetchall()

    # ---------- Theme library ----------

    def is_theme_used(self, theme: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM used_themes WHERE theme=? COLLATE NOCASE",
            (theme.strip(),),
        ).fetchone() is not None

    def add_custom_theme(self, theme: str) -> bool:
        theme = " ".join(theme.split()).strip()
        if not theme:
            return False
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    "INSERT INTO custom_themes(theme, enabled, created_at) VALUES(?,1,?)",
                    (theme, utc_now()),
                )
                return True
            except sqlite3.IntegrityError:
                self._conn.execute(
                    "UPDATE custom_themes SET enabled=1 WHERE theme=? COLLATE NOCASE",
                    (theme,),
                )
                return False

    def disable_theme(self, theme: str) -> None:
        theme = " ".join(theme.split()).strip()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO disabled_themes(theme, disabled_at) VALUES(?,?)",
                (theme, utc_now()),
            )

    def enable_theme(self, theme: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM disabled_themes WHERE theme=? COLLATE NOCASE",
                (theme.strip(),),
            )
            self._conn.execute(
                "UPDATE custom_themes SET enabled=1 WHERE theme=? COLLATE NOCASE",
                (theme.strip(),),
            )

    def theme_stats(self, built_in_count: int):
        custom_count = int(self._conn.execute(
            "SELECT COUNT(*) c FROM custom_themes WHERE enabled=1"
        ).fetchone()["c"])
        disabled_count = int(self._conn.execute(
            "SELECT COUNT(*) c FROM disabled_themes"
        ).fetchone()["c"])
        used_count = int(self._conn.execute(
            "SELECT COUNT(*) c FROM used_themes"
        ).fetchone()["c"])
        current_count = len(self.random_assignments())
        return {
            "built_in": built_in_count,
            "custom": custom_count,
            "disabled": disabled_count,
            "used": used_count,
            "current_assignments": current_count,
        }

    def recycle_used_history(self) -> int:
        with self._lock, self._conn:
            count = int(self._conn.execute(
                "SELECT COUNT(*) c FROM used_themes"
            ).fetchone()["c"])
            self._conn.execute("DELETE FROM used_themes")
            return count
