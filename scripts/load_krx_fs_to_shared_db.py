from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_FS_ROOT = Path("data/krx_fs")
DEFAULT_DB_PATH = Path("data/krx_shared_db/krx_shared_prices.sqlite")
DEFAULT_TABLE = "krx_fs_rows"
METADATA_COLUMNS = {
    "symbol": "TEXT NOT NULL",
    "filing_date": "TEXT",
    "report_year": "INTEGER",
    "report_name": "TEXT",
    "source_file": "TEXT NOT NULL",
    "source_row_number": "INTEGER NOT NULL",
    "loaded_at": "TEXT NOT NULL",
}


@dataclass(frozen=True)
class KRXFsLoadResult:
    file_count: int
    source_rows: int
    changed_rows: int
    stored_rows: int
    stored_files: int
    db_path: Path
    table: str


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load local KRX financial statement CSV rows into shared SQLite.")
    parser.add_argument("--fs-root", default=str(DEFAULT_FS_ROOT), help="Root directory containing symbol folders with KRX FS CSV files.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Target shared SQLite DB path.")
    parser.add_argument("--table", default=DEFAULT_TABLE, help="Target table name for raw KRX FS rows.")
    parser.add_argument("--batch-size", type=int, default=1000, help="SQLite executemany batch size.")
    return parser.parse_args()


def _quote_ident(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        return [str(col).strip() for col in next(reader, [])]


def _iter_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = [str(col).strip() for col in (reader.fieldnames or [])]
        rows = [{header: str(row.get(header) or "") for header in headers} for row in reader]
    return headers, rows


def _report_metadata(path: Path, fs_root: Path) -> dict[str, object]:
    symbol = path.parent.name.strip().upper()
    filing_date = ""
    report_year: int | None = None
    report_name = path.stem

    match = re.match(r"^(?P<date>\d{8})_(?P<name>.+)$", path.stem)
    if match:
        filing_date = f"{match.group('date')[:4]}-{match.group('date')[4:6]}-{match.group('date')[6:8]}"
        report_name = match.group("name")

    year_match = re.search(r"(20\d{2})", report_name)
    if year_match:
        report_year = int(year_match.group(1))

    return {
        "symbol": symbol.zfill(6) if symbol.isdigit() else symbol,
        "filing_date": filing_date,
        "report_year": report_year,
        "report_name": report_name,
        "source_file": path.relative_to(fs_root.parent).as_posix(),
    }


def _source_file_for_path(path: Path, fs_root: Path) -> str:
    try:
        return path.relative_to(fs_root.parent).as_posix()
    except ValueError:
        return path.as_posix()


def _ensure_table(conn: sqlite3.Connection, table: str, csv_columns: list[str]) -> None:
    columns_sql = [f"{_quote_ident(name)} {definition}" for name, definition in METADATA_COLUMNS.items()]
    columns_sql.extend(f"{_quote_ident(col)} TEXT" for col in csv_columns if col and col not in METADATA_COLUMNS)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_quote_ident(table)} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            {", ".join(columns_sql)},
            UNIQUE(source_file, source_row_number)
        )
        """
    )

    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({_quote_ident(table)})")}
    for name, definition in METADATA_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {_quote_ident(table)} ADD COLUMN {_quote_ident(name)} {definition}")
    for col in csv_columns:
        if col and col not in existing and col not in METADATA_COLUMNS:
            conn.execute(f"ALTER TABLE {_quote_ident(table)} ADD COLUMN {_quote_ident(col)} TEXT")

    conn.execute(f"CREATE INDEX IF NOT EXISTS {_quote_ident(f'idx_{table}_symbol')} ON {_quote_ident(table)}(symbol)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS {_quote_ident(f'idx_{table}_filing_date')} ON {_quote_ident(table)}(filing_date)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS {_quote_ident(f'idx_{table}_report_year')} ON {_quote_ident(table)}(report_year)")


def _insert_rows(
    conn: sqlite3.Connection,
    table: str,
    rows: list[dict[str, object]],
    columns: list[str],
    *,
    batch_size: int,
) -> int:
    if not rows:
        return 0

    insert_sql = (
        f"INSERT INTO {_quote_ident(table)} ({', '.join(_quote_ident(col) for col in columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)}) "
        "ON CONFLICT(source_file, source_row_number) DO UPDATE SET "
        + ", ".join(
            f"{_quote_ident(col)} = excluded.{_quote_ident(col)}"
            for col in columns
            if col not in {"source_file", "source_row_number"}
        )
    )

    changed = 0
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        before = conn.total_changes
        conn.executemany(insert_sql, [[row.get(col) for col in columns] for row in batch])
        changed += conn.total_changes - before
    return changed


def load_krx_fs_csv_files_to_shared_db(
    *,
    fs_root: Path,
    db_path: Path,
    table: str = DEFAULT_TABLE,
    csv_paths: list[Path] | None = None,
    batch_size: int = 1000,
) -> KRXFsLoadResult:
    if not fs_root.exists():
        raise FileNotFoundError(f"KRX FS root not found: {fs_root}")
    resolved_csv_paths = sorted(csv_paths if csv_paths is not None else fs_root.rglob("*.csv"))
    if not resolved_csv_paths:
        raise FileNotFoundError(f"No CSV files found under: {fs_root}")

    all_csv_columns: list[str] = []
    seen: set[str] = set()
    for path in resolved_csv_paths:
        for col in _read_header(path):
            if col and col not in seen:
                seen.add(col)
                all_csv_columns.append(col)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    loaded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    total_rows = 0
    changed_rows = 0

    with sqlite3.connect(db_path) as conn:
        _ensure_table(conn, table, all_csv_columns)
        conn.execute("BEGIN")
        try:
            for path in resolved_csv_paths:
                headers, file_rows = _iter_csv_rows(path)
                metadata = _report_metadata(path, fs_root)
                metadata["source_file"] = _source_file_for_path(path, fs_root)
                insert_rows: list[dict[str, object]] = []
                for row_number, row in enumerate(file_rows, start=1):
                    record = {
                        **metadata,
                        "source_row_number": row_number,
                        "loaded_at": loaded_at,
                    }
                    for col in all_csv_columns:
                        record[col] = row.get(col, "") if col in headers else ""
                    insert_rows.append(record)
                total_rows += len(insert_rows)
                changed_rows += _insert_rows(
                    conn,
                    table,
                    insert_rows,
                    [*METADATA_COLUMNS.keys(), *all_csv_columns],
                    batch_size=batch_size,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        stored_rows = conn.execute(f"SELECT COUNT(*) FROM {_quote_ident(table)}").fetchone()[0]
        stored_files = conn.execute(f"SELECT COUNT(DISTINCT source_file) FROM {_quote_ident(table)}").fetchone()[0]

    return KRXFsLoadResult(
        file_count=len(resolved_csv_paths),
        source_rows=total_rows,
        changed_rows=changed_rows,
        stored_rows=stored_rows,
        stored_files=stored_files,
        db_path=db_path,
        table=table,
    )


def main() -> int:
    args = _parse_args()
    result = load_krx_fs_csv_files_to_shared_db(
        fs_root=Path(args.fs_root),
        db_path=Path(args.db_path),
        table=str(args.table).strip() or DEFAULT_TABLE,
        batch_size=max(1, int(args.batch_size)),
    )
    print(
        "KRX FS load complete: "
        f"files={result.file_count} source_rows={result.source_rows} changed_rows={result.changed_rows} "
        f"stored_rows={result.stored_rows} stored_files={result.stored_files} db={result.db_path} table={result.table}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
