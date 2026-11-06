from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class MirrorState:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "mirror.sqlite3"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS google_calendar_mirror (
                    source_key TEXT PRIMARY KEY,
                    google_event_id TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                )
                """
            )

    def get(self, source_key: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM google_calendar_mirror WHERE source_key = ?",
                (source_key,),
            ).fetchone()

    def upsert(self, source_key: str, google_event_id: str, source_hash: str, last_seen_at: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO google_calendar_mirror (source_key, google_event_id, source_hash, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    google_event_id = excluded.google_event_id,
                    source_hash = excluded.source_hash,
                    last_seen_at = excluded.last_seen_at
                """,
                (source_key, google_event_id, source_hash, last_seen_at),
            )

    def stale_since(self, last_seen_at: str) -> Iterator[sqlite3.Row]:
        with self.connect() as conn:
            yield from conn.execute(
                "SELECT * FROM google_calendar_mirror WHERE last_seen_at < ?",
                (last_seen_at,),
            )

    def delete(self, source_key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM google_calendar_mirror WHERE source_key = ?", (source_key,))


class CalendarSnapshotState:
    """Minimal local state used to turn a complete EWS snapshot into tombstones.

    Only opaque source keys, hashes, event boundaries and cancellation state are
    persisted. Subjects, descriptions, locations and attendees are deliberately
    excluded from this reconciliation ledger.
    """

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "mirror.sqlite3"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS exchange_calendar_snapshot (
                    reconciliation_key TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    start_json TEXT NOT NULL,
                    end_json TEXT NOT NULL,
                    event_start TEXT NOT NULL,
                    event_end TEXT NOT NULL,
                    is_cancelled INTEGER NOT NULL DEFAULT 0,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (reconciliation_key, source_key)
                )
                """
            )

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def reconcile(
        self,
        reconciliation_key: str,
        observed: list[dict[str, Any]],
        window_start: datetime,
        window_end: datetime,
        observed_at: datetime,
        snapshot_complete: bool,
    ) -> list[dict[str, Any]]:
        """Record a snapshot and return active rows missing from a complete one."""

        observed_keys = {str(item["source_key"]) for item in observed}
        missing: list[dict[str, Any]] = []
        observed_iso = observed_at.astimezone(UTC).isoformat()
        window_start_utc = window_start.astimezone(UTC)
        window_end_utc = window_end.astimezone(UTC)

        with self.connect() as conn:
            if snapshot_complete:
                previous = conn.execute(
                    """
                    SELECT * FROM exchange_calendar_snapshot
                    WHERE reconciliation_key = ? AND is_cancelled = 0
                    """,
                    (reconciliation_key,),
                ).fetchall()
                for row in previous:
                    if row["source_key"] in observed_keys:
                        continue
                    event_start = self._parse_datetime(row["event_start"])
                    event_end = self._parse_datetime(row["event_end"])
                    if event_start < window_end_utc and event_end > window_start_utc:
                        missing.append(dict(row))
                        conn.execute(
                            """
                            UPDATE exchange_calendar_snapshot
                            SET is_cancelled = 1
                            WHERE reconciliation_key = ? AND source_key = ?
                            """,
                            (reconciliation_key, row["source_key"]),
                        )

            for item in observed:
                conn.execute(
                    """
                    INSERT INTO exchange_calendar_snapshot (
                        reconciliation_key, source_key, source_hash, start_json,
                        end_json, event_start, event_end, is_cancelled, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(reconciliation_key, source_key) DO UPDATE SET
                        source_hash = excluded.source_hash,
                        start_json = excluded.start_json,
                        end_json = excluded.end_json,
                        event_start = excluded.event_start,
                        event_end = excluded.event_end,
                        is_cancelled = excluded.is_cancelled,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        reconciliation_key,
                        item["source_key"],
                        item["source_hash"],
                        item["start_json"],
                        item["end_json"],
                        item["event_start"],
                        item["event_end"],
                        int(bool(item["is_cancelled"])),
                        observed_iso,
                    ),
                )

            # Events that naturally aged out of the polling window are not
            # deletions and no longer need reconciliation state.
            conn.execute(
                """
                DELETE FROM exchange_calendar_snapshot
                WHERE reconciliation_key = ? AND event_end <= ?
                """,
                (reconciliation_key, window_start_utc.isoformat()),
            )
            conn.execute(
                """
                DELETE FROM exchange_calendar_snapshot
                WHERE last_seen_at < ?
                """,
                ((observed_at.astimezone(UTC) - timedelta(days=120)).isoformat(),),
            )

        return missing
