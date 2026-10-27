"""Lightweight SQLite audit log for AgentFence.

Every command that reaches the firewall is recorded with a UTC timestamp, the
final status, the rule that triggered the decision and (when executed) the
exit code of the process.
"""

from __future__ import annotations

import getpass
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Union


class DatabaseError(RuntimeError):
    """Raised when the audit database cannot be opened or written."""


class Status(str, Enum):
    """Final outcome of a command."""

    ALLOWED = "ALLOWED"      # matched an allow rule / default allow, executed
    CONFIRMED = "CONFIRMED"  # human approved, executed
    DENIED = "DENIED"        # human (or missing TTY) refused, not executed
    BLOCKED = "BLOCKED"      # blocked by a rule, not executed


_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT    NOT NULL,
    command    TEXT    NOT NULL,
    status     TEXT    NOT NULL,
    rule_name  TEXT,
    reason     TEXT,
    exit_code  INTEGER,
    cwd        TEXT,
    user       TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log (timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_status ON audit_log (status);
"""


@dataclass(frozen=True)
class AuditEntry:
    id: int
    timestamp: datetime
    command: str
    status: Status
    rule_name: Optional[str]
    reason: Optional[str]
    exit_code: Optional[int]
    cwd: Optional[str]
    user: Optional[str]


def _current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # getpass can fail in containers without a passwd entry
        return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


class AuditDatabase:
    """Thin wrapper around a SQLite file."""

    def __init__(self, path: Union[str, Path]) -> None:
        self.path = Path(path).expanduser()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
        except (OSError, sqlite3.Error) as exc:
            raise DatabaseError(f"Cannot open audit database at {self.path}: {exc}") from exc

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ write
    def log(
        self,
        command: str,
        status: Status,
        rule_name: Optional[str] = None,
        reason: Optional[str] = None,
        exit_code: Optional[int] = None,
    ) -> int:
        """Insert one audit record and return its id."""
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    "INSERT INTO audit_log "
                    "(timestamp, command, status, rule_name, reason, exit_code, cwd, user) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        command,
                        Status(status).value,
                        rule_name,
                        reason,
                        exit_code,
                        os.getcwd(),
                        _current_user(),
                    ),
                )
                return int(cursor.lastrowid)
        except (OSError, sqlite3.Error) as exc:
            raise DatabaseError(f"Failed to write audit record: {exc}") from exc

    def set_exit_code(self, entry_id: int, exit_code: int) -> None:
        """Attach the process exit code to an existing record."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE audit_log SET exit_code = ? WHERE id = ?",
                    (exit_code, entry_id),
                )
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to update audit record {entry_id}: {exc}") from exc

    # ------------------------------------------------------------------- read
    def history(self, limit: int = 20, status: Optional[Status] = None) -> List[AuditEntry]:
        """Return the most recent records (newest first)."""
        if limit < 1:
            raise ValueError("limit must be >= 1")
        query = "SELECT * FROM audit_log"
        params: list = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(Status(status).value)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as conn:
                rows = conn.execute(query, params).fetchall()
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to read audit history: {exc}") from exc
        return [self._row_to_entry(row) for row in rows]

    def stats(self) -> Dict[str, int]:
        """Return the number of records per status (all statuses present)."""
        counts = {status.value: 0 for status in Status}
        try:
            with self._connect() as conn:
                for row in conn.execute("SELECT status, COUNT(*) AS n FROM audit_log GROUP BY status"):
                    counts[row["status"]] = row["n"]
        except sqlite3.Error as exc:
            raise DatabaseError(f"Failed to read audit stats: {exc}") from exc
        return counts

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> AuditEntry:
        return AuditEntry(
            id=row["id"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            command=row["command"],
            status=Status(row["status"]),
            rule_name=row["rule_name"],
            reason=row["reason"],
            exit_code=row["exit_code"],
            cwd=row["cwd"],
            user=row["user"],
        )