"""SQLite-backed result tables.

Scraped rows land here instead of in the conversation, so a 10,000-row job can be
filtered and aggregated without any of it entering the model's context.
"""

from __future__ import annotations

import csv
import difflib
import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_TABLE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
_WRITE_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "attach",
    "detach",
    "pragma",
    "vacuum",
    "reindex",
    "truncate",
)
"""Defence in depth only — the read-only connection is the actual guarantee.

`replace` is deliberately absent: REPLACE(str, from, to) is an ordinary read-only
string function, and `REPLACE INTO` cannot appear here anyway because a statement
must already begin with SELECT or WITH.
"""

_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
"""Literals are blanked before keyword scanning, so `WHERE action = 'delete'` and
`LIKE '%update%'` are not mistaken for write statements."""
_DEFAULT_MAX_ROWS = 200
_MAX_MATERIALISED_ROWS = 100_000
_EXPORT_FORMATS = ("csv", "json")
_MAX_DIFF_LINES = 200
"""Diffs are summaries, not payloads; a full one can be larger than the page."""


class StoreError(Exception):
    """Raised for invalid table names, unknown tables, or rejected queries."""


@dataclass
class TableInfo:
    name: str
    row_count: int
    columns: list[str]
    sample: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class QueryResult:
    rows: list[dict[str, Any]]
    columns: list[str]
    truncated: bool
    total_rows_examined: int


class ResultStore:
    def __init__(self, db_path: Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open, commit-or-rollback, and *close*.

        sqlite3's own connection context manager commits but never closes, so
        `with self._connect() as conn` leaked a handle on every store call —
        list_tables leaked N+1.
        """
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _connect_readonly(self) -> sqlite3.Connection:  # caller closes explicitly
        # A genuine read-only handle: SQLite itself refuses writes, so this holds
        # even if a write statement slips past the keyword check.
        conn = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, table: str, rows: list[dict[str, Any]]) -> TableInfo:
        """Replace `table` with `rows`."""
        name = _validate_name(table)
        if not rows:
            raise StoreError(f"Cannot save table {name!r}: no rows to write")
        columns = _union_columns(rows)
        with self._connect() as conn:
            conn.execute(f'DROP TABLE IF EXISTS "{name}"')
            conn.execute(f'CREATE TABLE "{name}" ({_column_ddl(columns, rows)})')
            _insert(conn, name, columns, rows)
        return self.get_table(name, sample=0)

    def append(self, table: str, rows: list[dict[str, Any]]) -> TableInfo:
        name = _validate_name(table)
        if not rows:
            raise StoreError(f"Cannot append to {name!r}: no rows to write")
        if not self._exists(name):
            return self.save(name, rows)
        existing = self.get_table(name, sample=0).columns
        with self._connect() as conn:
            for column in _union_columns(rows):
                if column not in existing:
                    conn.execute(f'ALTER TABLE "{name}" ADD COLUMN "{column}" TEXT')
                    existing.append(column)
            _insert(conn, name, existing, rows)
        return self.get_table(name, sample=0)

    def list_tables(self) -> list[TableInfo]:
        with self._connect() as conn:
            names = [
                str(r["name"])
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '\\_%' ESCAPE '\\' "
                    "ORDER BY name"
                )
            ]
        return [self.get_table(n, sample=0) for n in names]

    def get_table(self, table: str, sample: int = 5) -> TableInfo:
        name = _validate_name(table)
        if not self._exists(name):
            raise StoreError(f"Table {name!r} not found. Use list_tables to see what exists.")
        with self._connect() as conn:
            columns = [str(r["name"]) for r in conn.execute(f'PRAGMA table_info("{name}")')]
            count = int(conn.execute(f'SELECT COUNT(*) AS n FROM "{name}"').fetchone()["n"])
            rows: list[dict[str, Any]] = []
            if sample > 0:
                rows = [dict(r) for r in conn.execute(f'SELECT * FROM "{name}" LIMIT ?', (sample,))]
        return TableInfo(name=name, row_count=count, columns=columns, sample=rows)

    def query(self, sql: str, max_rows: int = _DEFAULT_MAX_ROWS) -> QueryResult:
        """Run a read-only SELECT and return at most `max_rows` rows."""
        statement = _validate_select(sql)
        conn = self._connect_readonly()
        try:
            cursor = conn.execute(statement)
            fetched = cursor.fetchmany(max_rows + 1)
            columns = [d[0] for d in (cursor.description or [])]
        except sqlite3.Error as exc:
            raise StoreError(f"Query failed: {exc}") from None
        finally:
            conn.close()
        truncated = len(fetched) > max_rows
        rows = [dict(r) for r in fetched[:max_rows]]
        return QueryResult(
            rows=rows,
            columns=columns,
            truncated=truncated,
            total_rows_examined=len(rows),
        )

    def query_to_table(
        self, sql: str, target: str, max_rows: int = _MAX_MATERIALISED_ROWS
    ) -> tuple[TableInfo, bool, bool]:
        """Persist a SELECT's results as `target`.

        Returns (info, replaced, truncated). Because SQL already expresses renaming,
        merging, deduplicating and filtering, this one operation covers the whole
        family of table-cleanup edits without ever exposing a write statement.
        """
        name = _validate_name(target)
        statement = _validate_select(sql)

        # Materialise fully through the read-only handle and close it before writing,
        # so a query that reads the table it targets cannot read its own partial write.
        conn = self._connect_readonly()
        try:
            cursor = conn.execute(statement)
            fetched = cursor.fetchmany(max_rows + 1)
        except sqlite3.Error as exc:
            raise StoreError(f"Query failed: {exc}") from None
        finally:
            conn.close()

        truncated = len(fetched) > max_rows
        rows = [dict(r) for r in fetched[:max_rows]]
        if not rows:
            raise StoreError("Query returned no rows, so there is nothing to save.")

        replaced = self._exists(name)
        return self.save(name, rows), replaced, truncated

    def export(self, table: str, fmt: str, dest_dir: Path) -> Path:
        name = _validate_name(table)
        normalised = fmt.lower().strip()
        if normalised not in _EXPORT_FORMATS:
            raise StoreError(f"Unsupported export format {fmt!r}; use one of: csv, json")
        info = self.get_table(name, sample=0)
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / f"{name}.{normalised}"
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(f'SELECT * FROM "{name}"')]
        if normalised == "json":
            target.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            with target.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=info.columns)
                writer.writeheader()
                writer.writerows(rows)
        return target

    def drop(self, table: str) -> None:
        name = _validate_name(table)
        with self._connect() as conn:
            conn.execute(f'DROP TABLE IF EXISTS "{name}"')

    def _exists(self, name: str) -> bool:
        with self._connect() as conn:
            found = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
        return found is not None


def _validate_name(table: str) -> str:
    name = (table or "").strip()
    if not _TABLE_NAME_RE.match(name):
        raise StoreError(
            f"Invalid table name {table!r}. Use letters, digits and underscores, "
            "starting with a letter (max 63 characters)."
        )
    return name


def _validate_select(sql: str) -> str:
    statement = (sql or "").strip().rstrip(";").strip()
    if not statement:
        raise StoreError("Query is empty; provide a SELECT statement.")
    if ";" in statement:
        raise StoreError("Provide a single SELECT statement; multiple statements are rejected.")
    lowered = statement.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise StoreError("Only SELECT queries are allowed; this tool never modifies saved data.")
    scannable = _STRING_LITERAL_RE.sub("''", lowered)
    for keyword in _WRITE_KEYWORDS:
        if re.search(rf"\b{keyword}\b", scannable):
            raise StoreError(
                f"Rejected: {keyword.upper()} is not permitted. query_table is read-only."
            )
    return statement


def _union_columns(rows: list[dict[str, Any]]) -> list[str]:
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    return columns


def _sqlite_type(values: list[Any]) -> str:
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            return "INTEGER"
        if isinstance(value, int):
            return "INTEGER"
        if isinstance(value, float):
            return "REAL"
        return "TEXT"
    return "TEXT"


def _column_ddl(columns: list[str], rows: list[dict[str, Any]]) -> str:
    # Types matter: if every column is TEXT then `WHERE price > 6` compares strings
    # and silently returns the wrong rows.
    return ", ".join(f'"{c}" {_sqlite_type([row.get(c) for row in rows])}' for c in columns)


def _insert(
    conn: sqlite3.Connection, name: str, columns: list[str], rows: list[dict[str, Any]]
) -> None:
    placeholders = ", ".join("?" for _ in columns)
    quoted = ", ".join(f'"{c}"' for c in columns)
    conn.executemany(
        f'INSERT INTO "{name}" ({quoted}) VALUES ({placeholders})',
        [tuple(_coerce(row.get(c)) for c in columns) for row in rows],
    )


def _coerce(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, bool):
        return int(value)
    return json.dumps(value, ensure_ascii=False)


_SNAPSHOT_TABLE = "_snapshots"
"""Underscore-prefixed so it does not appear in list_tables alongside result data."""


@dataclass
class Change:
    url: str
    status: str
    """new, same, or changed."""
    previous_seen_at: str | None
    added_lines: int
    removed_lines: int
    diff: str


class SnapshotStore:
    """Content snapshots, so a re-scrape can say what actually changed.

    This is the monitoring case without a scheduler: fetch now, fetch again next
    week, and get a diff rather than two walls of text to compare by eye.
    """

    def __init__(self, store: ResultStore) -> None:
        self._store = store
        with self._store._connect() as conn:
            conn.execute(
                f'CREATE TABLE IF NOT EXISTS "{_SNAPSHOT_TABLE}" ('
                "url TEXT PRIMARY KEY, content TEXT NOT NULL, "
                "digest TEXT NOT NULL, seen_at TEXT NOT NULL)"
            )

    def compare(self, url: str, content: str, *, seen_at: str, context: int = 2) -> Change:
        """Diff `content` against the stored snapshot and replace it."""
        digest = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()
        with self._store._connect() as conn:
            row = conn.execute(
                f'SELECT content, digest, seen_at FROM "{_SNAPSHOT_TABLE}" WHERE url = ?', (url,)
            ).fetchone()

        if row is None:
            status, previous, added, removed, diff = "new", None, 0, 0, ""
        elif str(row["digest"]) == digest:
            status, previous, added, removed, diff = "same", str(row["seen_at"]), 0, 0, ""
        else:
            previous = str(row["seen_at"])
            status = "changed"
            added, removed, diff = _diff(str(row["content"]), content, context)

        with self._store._connect() as conn:
            conn.execute(
                f'INSERT INTO "{_SNAPSHOT_TABLE}" (url, content, digest, seen_at) '
                "VALUES (?, ?, ?, ?) ON CONFLICT(url) DO UPDATE SET "
                "content=excluded.content, digest=excluded.digest, seen_at=excluded.seen_at",
                (url, content, digest, seen_at),
            )
        return Change(
            url=url,
            status=status,
            previous_seen_at=previous,
            added_lines=added,
            removed_lines=removed,
            diff=diff,
        )

    def forget(self, url: str) -> bool:
        with self._store._connect() as conn:
            cursor = conn.execute(f'DELETE FROM "{_SNAPSHOT_TABLE}" WHERE url = ?', (url,))
            return cursor.rowcount > 0

    def tracked(self) -> list[dict[str, Any]]:
        with self._store._connect() as conn:
            return [
                {"url": str(r["url"]), "seen_at": str(r["seen_at"]), "size": len(str(r["content"]))}
                for r in conn.execute(
                    f'SELECT url, seen_at, content FROM "{_SNAPSHOT_TABLE}" ORDER BY url'
                )
            ]


def _diff(before: str, after: str, context: int) -> tuple[int, int, str]:
    lines = list(
        difflib.unified_diff(
            before.splitlines(), after.splitlines(), "before", "after", n=context, lineterm=""
        )
    )
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    body = "\n".join(lines[:_MAX_DIFF_LINES])
    if len(lines) > _MAX_DIFF_LINES:
        body += f"\n[diff truncated: {len(lines)} lines total]"
    return added, removed, body
