"""SQLite-backed result tables.

Scraped rows land here instead of in the conversation, so a 10,000-row job can be
filtered and aggregated without any of it entering the model's context.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
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
    "replace",
    "attach",
    "detach",
    "pragma",
    "vacuum",
    "reindex",
    "truncate",
)
_DEFAULT_MAX_ROWS = 200
_MAX_MATERIALISED_ROWS = 100_000
_EXPORT_FORMATS = ("csv", "json")


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

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _connect_readonly(self) -> sqlite3.Connection:
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
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name"
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
    for keyword in _WRITE_KEYWORDS:
        if re.search(rf"\b{keyword}\b", lowered):
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
