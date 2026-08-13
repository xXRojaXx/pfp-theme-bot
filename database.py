from __future__ import annotations

import random
import threading
from datetime import datetime, timezone
from typing import Iterable

import psycopg
from psycopg.rows import dict_row


MAX_SUGGESTIONS = 25


def utc_now():
    return datetime.now(timezone.utc)


class Database:
    """PostgreSQL storage for the PFP Theme Bot.

    Northflank can inject either DATABASE_URL or POSTGRES_URI.
    The bot uses a new short-lived connection per operation, which is simple
    and resilient for this small Discord workload.
    """

    def __init__(self, url: str):
        if not url:
            raise RuntimeError(
                "Database URL is missing. Set DATABASE_URL or POSTGRES_URI."
            )
        self.url = url
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self):
        return psycopg.connect(self.url, row_factory=dict_row)

    def _init_schema(self) -> None:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS group_rounds (
                id BIGSERIAL PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TIMESTAMPTZ NOT NULL,
                closed_at TIMESTAMPTZ
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS suggestions (
                id BIGSERIAL PRIMARY KEY,
                round_id BIGINT NOT NULL REFERENCES group_rounds(id) ON DELETE CASCADE,
                theme TEXT NOT NULL,
                suggested_by BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_suggestion_round_theme
            ON suggestions(round_id, lower(theme))
            """,
            """
            CREATE TABLE IF NOT EXISTS votes (
                round_id BIGINT NOT NULL REFERENCES group_rounds(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                suggestion_id BIGINT NOT NULL REFERENCES suggestions(id) ON DELETE CASCADE,
                updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY(round_id, user_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS group_history (
                id BIGSERIAL PRIMARY KEY,
                round_id BIGINT NOT NULL,
                theme TEXT NOT NULL,
                votes INTEGER NOT NULL DEFAULT 0,
                closed_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS used_themes (
                id BIGSERIAL PRIMARY KEY,
                theme TEXT NOT NULL,
                used_at TIMESTAMPTZ NOT NULL,
                source TEXT NOT NULL,
                round_id BIGINT
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_used_theme_lower
            ON used_themes(lower(theme))
            """,
            """
            CREATE TABLE IF NOT EXISTS custom_themes (
                id BIGSERIAL PRIMARY KEY,
                theme TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_custom_theme_lower
            ON custom_themes(lower(theme))
            """,
            """
            CREATE TABLE IF NOT EXISTS disabled_themes (
                id BIGSERIAL PRIMARY KEY,
                theme TEXT NOT NULL,
                disabled_at TIMESTAMPTZ NOT NULL
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_disabled_theme_lower
            ON disabled_themes(lower(theme))
            """,
        ]

        with self._connect() as conn:
            with conn.cursor() as cur:
                for statement in statements:
                    cur.execute(statement)

    # ---------- Group election ----------

    def active_group_round(self):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM group_rounds
                    WHERE status != 'closed'
                    ORDER BY id DESC
                    LIMIT 1
                    """
                )
                return cur.fetchone()

    def create_group_round(self) -> int:
        with self._lock:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    # Serialise creation so two admins cannot open two rounds at once.
                    cur.execute("SELECT pg_advisory_xact_lock(93451001)")
                    cur.execute(
                        """
                        SELECT id
                        FROM group_rounds
                        WHERE status != 'closed'
                        ORDER BY id DESC
                        LIMIT 1
                        """
                    )
                    active = cur.fetchone()
                    if active:
                        return int(active["id"])

                    cur.execute(
                        """
                        INSERT INTO group_rounds(status, created_at)
                        VALUES('open', %s)
                        RETURNING id
                        """,
                        (utc_now(),),
                    )
                    return int(cur.fetchone()["id"])

    def set_group_status(self, round_id: int, status: str) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE group_rounds SET status=%s WHERE id=%s",
                    (status, round_id),
                )

    def suggestion_count(self, round_id: int) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS c FROM suggestions WHERE round_id=%s",
                    (round_id,),
                )
                return int(cur.fetchone()["c"])

    def add_suggestion(self, round_id: int, theme: str, user_id: int) -> bool:
        theme = " ".join(theme.split()).strip()
        if not theme:
            return False

        with self._lock:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(%s)", (round_id,))

                    cur.execute(
                        "SELECT status FROM group_rounds WHERE id=%s",
                        (round_id,),
                    )
                    round_row = cur.fetchone()
                    if not round_row or round_row["status"] == "closed":
                        return False

                    cur.execute(
                        "SELECT 1 FROM used_themes WHERE lower(theme)=lower(%s) LIMIT 1",
                        (theme,),
                    )
                    if cur.fetchone():
                        return False

                    cur.execute(
                        "SELECT COUNT(*) AS c FROM suggestions WHERE round_id=%s",
                        (round_id,),
                    )
                    if int(cur.fetchone()["c"]) >= MAX_SUGGESTIONS:
                        return False

                    cur.execute(
                        """
                        INSERT INTO suggestions(round_id, theme, suggested_by, created_at)
                        VALUES(%s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        RETURNING id
                        """,
                        (round_id, theme, user_id, utc_now()),
                    )
                    return cur.fetchone() is not None

    def add_random_suggestion(
        self,
        round_id: int,
        user_id: int,
        built_in_themes: Iterable[str],
    ):
        """Pick and add one random eligible theme atomically.

        The PostgreSQL advisory lock guarantees that simultaneous /pfp random
        calls in the same election cannot reserve the same theme.
        """
        with self._lock:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(%s)", (round_id,))

                    cur.execute(
                        "SELECT status FROM group_rounds WHERE id=%s",
                        (round_id,),
                    )
                    round_row = cur.fetchone()
                    if not round_row or round_row["status"] == "closed":
                        return None

                    cur.execute(
                        "SELECT COUNT(*) AS c FROM suggestions WHERE round_id=%s",
                        (round_id,),
                    )
                    if int(cur.fetchone()["c"]) >= MAX_SUGGESTIONS:
                        return None

                    cur.execute(
                        "SELECT theme FROM suggestions WHERE round_id=%s",
                        (round_id,),
                    )
                    current = {
                        row["theme"].casefold().strip()
                        for row in cur.fetchall()
                    }

                    cur.execute(
                        "SELECT theme FROM custom_themes WHERE enabled=TRUE"
                    )
                    custom = [row["theme"] for row in cur.fetchall()]

                    cur.execute("SELECT theme FROM disabled_themes")
                    disabled = {
                        row["theme"].casefold().strip()
                        for row in cur.fetchall()
                    }

                    cur.execute("SELECT theme FROM used_themes")
                    used = {
                        row["theme"].casefold().strip()
                        for row in cur.fetchall()
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
                            or key in used
                        ):
                            continue
                        seen.add(key)
                        pool.append(theme.strip())

                    if not pool:
                        return None

                    # Extremely unlikely to need more than one attempt, but loop
                    # defensively in case a case-insensitive unique constraint
                    # rejects a candidate added elsewhere.
                    rng = random.SystemRandom()
                    while pool:
                        theme = rng.choice(pool)
                        cur.execute(
                            """
                            INSERT INTO suggestions(round_id, theme, suggested_by, created_at)
                            VALUES(%s, %s, %s, %s)
                            ON CONFLICT DO NOTHING
                            RETURNING id
                            """,
                            (round_id, theme, user_id, utc_now()),
                        )
                        inserted = cur.fetchone()
                        if inserted:
                            return theme
                        pool.remove(theme)

                    return None

    def get_suggestions(self, round_id: int):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        s.id,
                        s.theme,
                        s.suggested_by,
                        COUNT(v.user_id)::INTEGER AS vote_count
                    FROM suggestions s
                    LEFT JOIN votes v
                        ON v.suggestion_id = s.id
                        AND v.round_id = s.round_id
                    WHERE s.round_id=%s
                    GROUP BY s.id, s.theme, s.suggested_by
                    ORDER BY s.id ASC
                    """,
                    (round_id,),
                )
                return cur.fetchall()

    def cast_vote(self, round_id: int, user_id: int, suggestion_id: int) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.id
                    FROM suggestions s
                    JOIN group_rounds r ON r.id=s.round_id
                    WHERE s.id=%s AND s.round_id=%s AND r.status!='closed'
                    """,
                    (suggestion_id, round_id),
                )
                if not cur.fetchone():
                    return False

                cur.execute(
                    """
                    INSERT INTO votes(round_id, user_id, suggestion_id, updated_at)
                    VALUES(%s, %s, %s, %s)
                    ON CONFLICT(round_id, user_id)
                    DO UPDATE SET
                        suggestion_id=EXCLUDED.suggestion_id,
                        updated_at=EXCLUDED.updated_at
                    """,
                    (round_id, user_id, suggestion_id, utc_now()),
                )
                return True

    def get_user_vote(self, round_id: int, user_id: int):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT s.theme
                    FROM votes v
                    JOIN suggestions s ON s.id=v.suggestion_id
                    WHERE v.round_id=%s AND v.user_id=%s
                    """,
                    (round_id, user_id),
                )
                return cur.fetchone()

    def close_group_round(self, round_id: int):
        with self._lock:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_xact_lock(%s)", (round_id,))
                    cur.execute(
                        "SELECT status FROM group_rounds WHERE id=%s",
                        (round_id,),
                    )
                    round_row = cur.fetchone()
                    if not round_row or round_row["status"] == "closed":
                        return None

                    cur.execute(
                        """
                        SELECT
                            s.id,
                            s.theme,
                            COUNT(v.user_id)::INTEGER AS vote_count
                        FROM suggestions s
                        LEFT JOIN votes v
                            ON v.suggestion_id=s.id
                            AND v.round_id=s.round_id
                        WHERE s.round_id=%s
                        GROUP BY s.id, s.theme
                        ORDER BY s.id
                        """,
                        (round_id,),
                    )
                    rows = cur.fetchall()
                    if not rows:
                        return None

                    max_votes = max(int(row["vote_count"]) for row in rows)
                    finalists = [
                        row for row in rows
                        if int(row["vote_count"]) == max_votes
                    ]
                    winner = random.SystemRandom().choice(finalists)
                    now = utc_now()

                    cur.execute(
                        """
                        UPDATE group_rounds
                        SET status='closed', closed_at=%s
                        WHERE id=%s
                        """,
                        (now, round_id),
                    )
                    cur.execute(
                        """
                        INSERT INTO group_history(round_id, theme, votes, closed_at)
                        VALUES(%s, %s, %s, %s)
                        """,
                        (round_id, winner["theme"], max_votes, now),
                    )
                    cur.execute(
                        """
                        INSERT INTO used_themes(theme, used_at, source, round_id)
                        VALUES(%s, %s, 'group_vote', %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (winner["theme"], now, round_id),
                    )

                    return {
                        "theme": winner["theme"],
                        "votes": max_votes,
                        "tie": len(finalists) > 1,
                        "finalists": [row["theme"] for row in finalists],
                    }

    def group_history(self, limit: int = 20):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM group_history
                    ORDER BY id DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                return cur.fetchall()

    # ---------- Theme library ----------

    def is_theme_used(self, theme: str) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1
                    FROM used_themes
                    WHERE lower(theme)=lower(%s)
                    LIMIT 1
                    """,
                    (theme.strip(),),
                )
                return cur.fetchone() is not None

    def add_custom_theme(self, theme: str) -> bool:
        theme = " ".join(theme.split()).strip()
        if not theme:
            return False

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE custom_themes
                    SET enabled=TRUE
                    WHERE lower(theme)=lower(%s)
                    RETURNING id
                    """,
                    (theme,),
                )
                if cur.fetchone():
                    return False

                cur.execute(
                    """
                    INSERT INTO custom_themes(theme, enabled, created_at)
                    VALUES(%s, TRUE, %s)
                    RETURNING id
                    """,
                    (theme, utc_now()),
                )
                return cur.fetchone() is not None

    def disable_theme(self, theme: str) -> None:
        theme = " ".join(theme.split()).strip()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM disabled_themes WHERE lower(theme)=lower(%s)",
                    (theme,),
                )
                cur.execute(
                    """
                    INSERT INTO disabled_themes(theme, disabled_at)
                    VALUES(%s, %s)
                    """,
                    (theme, utc_now()),
                )

    def enable_theme(self, theme: str) -> None:
        theme = " ".join(theme.split()).strip()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM disabled_themes WHERE lower(theme)=lower(%s)",
                    (theme,),
                )
                cur.execute(
                    """
                    UPDATE custom_themes
                    SET enabled=TRUE
                    WHERE lower(theme)=lower(%s)
                    """,
                    (theme,),
                )

    def theme_stats(self, built_in_count: int):
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) AS c FROM custom_themes WHERE enabled=TRUE"
                )
                custom_count = int(cur.fetchone()["c"])

                cur.execute("SELECT COUNT(*) AS c FROM disabled_themes")
                disabled_count = int(cur.fetchone()["c"])

                cur.execute("SELECT COUNT(*) AS c FROM used_themes")
                used_count = int(cur.fetchone()["c"])

                return {
                    "built_in": built_in_count,
                    "custom": custom_count,
                    "disabled": disabled_count,
                    "used": used_count,
                }

    def recycle_used_history(self) -> int:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM used_themes")
                count = int(cur.fetchone()["c"])
                cur.execute("DELETE FROM used_themes")
                return count
