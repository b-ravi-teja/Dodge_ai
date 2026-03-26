from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from db import DEFAULT_DB_PATH, get_connection


REPO_ROOT = Path(__file__).resolve().parents[1]

REQUESTED_DATA_DIR = REPO_ROOT / "data" / "sap-02c-data"
FALLBACK_DATA_DIR = REPO_ROOT / "data"
DATA_DIR = REQUESTED_DATA_DIR if REQUESTED_DATA_DIR.exists() else FALLBACK_DATA_DIR


FK_COLUMNS_CAMEL_CASE = {
    # Used by `backend/graph_builder.py` for edge inference.
    "soldToParty",
    "customer",
    "material",
    "referenceSdDocument",
    "referenceSdDocumentItem",
    "salesDocument",
    "deliveryDocument",
    "billingDocument",
    "invoiceReference",
    "referenceDocument",
    "accountingDocument",
    "clearingAccountingDocument",
    "clearingDocFiscalYear",
    "cancelledBillingDocument",
    # Common FK columns that also appear as references in tables.
    "salesOrder",
    "deliveryDocument",
}


_CAMEL_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_2 = re.compile(r"([a-z0-9])([A-Z])")


def to_snake_case(s: str) -> str:
    s = s.strip()
    s = s.replace("-", "_").replace(" ", "_")
    s = _CAMEL_1.sub(r"\1_\2", s)
    s = _CAMEL_2.sub(r"\1_\2", s)
    s = re.sub(r"__+", "_", s)
    return s.lower()


def _iter_table_dirs(base_dir: Path) -> Iterable[Path]:
    for child in sorted(base_dir.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            yield child


def _read_part(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    if path.suffix.lower() == ".jsonl":
        return pd.read_json(path, lines=True)
    raise ValueError(f"Unsupported file extension: {path.suffix}")


def _load_table(table_dir: Path) -> pd.DataFrame:
    csv_parts = sorted(table_dir.glob("*.csv"))
    if csv_parts:
        parts = csv_parts
    else:
        parts = sorted(table_dir.glob("*.jsonl"))

    if not parts:
        raise FileNotFoundError(f"No .csv or .jsonl files in {table_dir}")

    dfs: List[pd.DataFrame] = []
    for fp in parts:
        df = _read_part(fp)
        dfs.append(df)

    if not dfs:
        raise RuntimeError(f"No dataframes loaded for {table_dir}")

    # Concat across parts for the same table.
    return pd.concat(dfs, ignore_index=True, sort=False)


def _sanitize_df_for_sql(df: pd.DataFrame) -> pd.DataFrame:
    """
    SQLite via `pandas.to_sql` requires bindable scalar types.
    Some of the dataset fields can be dict/list (from JSONL), so we serialize them.
    """

    def sanitize_cell(v: object) -> object:
        if v is None:
            return None
        # pandas NA / NaN
        try:
            if pd.isna(v):  # type: ignore[arg-type]
                return None
        except Exception:
            pass

        # Normalize numpy scalars -> python scalars
        if hasattr(v, "item"):
            try:
                return v.item()  # type: ignore[no-any-return]
            except Exception:
                pass

        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False, sort_keys=True)

        if isinstance(v, pd.Timestamp):
            return v.isoformat()

        # Ensure SQLite-compatible primitives.
        if isinstance(v, (str, int, float, bool)):
            return v

        return str(v)

    # Only object columns typically contain complex values; keep it cheap.
    obj_cols = [c for c in df.columns if df[c].dtype == "object"]
    if obj_cols:
        for c in obj_cols:
            df[c] = df[c].map(sanitize_cell)
    return df


def _create_foreign_key_indexes(conn: sqlite3.Connection, table_name: str, columns: Sequence[str]) -> None:
    fk_candidates = {to_snake_case(c) for c in FK_COLUMNS_CAMEL_CASE}
    for col in columns:
        # Heuristic: treat known FK columns and *id columns (except literal `id`) as FK-like.
        is_fk = (col in fk_candidates) or (col.endswith("_id") and col != "id")
        if not is_fk:
            continue
        idx_name = f"idx_{table_name}_{col}"
        # SQLite index names are limited; keep deterministic but short.
        idx_name = idx_name[:60]

        conn.execute(f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON "{table_name}" ("{col}")')


def load_all_tables_to_sqlite(db_path: Path = DEFAULT_DB_PATH) -> None:
    if not DATA_DIR.exists():
        raise FileNotFoundError(f"Dataset dir not found: {DATA_DIR}")

    print(f"Loading dataset from: {DATA_DIR}")
    print(f"Writing SQLite DB to: {db_path}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")

        # Ensure idempotence: each table is replaced on every run.
        with conn:
            for table_dir in _iter_table_dirs(DATA_DIR):
                # Use directory name as table name; normalize to snake_case.
                table_name = to_snake_case(table_dir.name)

                df = _load_table(table_dir)
                df.columns = [to_snake_case(str(c)) for c in df.columns]
                df = _sanitize_df_for_sql(df)

                # Replace table (drop+create) to avoid duplicates on reruns.
                conn.execute(f'DROP TABLE IF EXISTS "{table_name}";')
                df.to_sql(table_name, conn, if_exists="replace", index=False)

                _create_foreign_key_indexes(conn, table_name, df.columns)
    finally:
        conn.close()

    # Print final list of tables and row counts.
    conn2 = get_connection(db_path)
    try:
        rows = conn2.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()

        print("\nTables and row counts:")
        for r in rows:
            table_name = r["name"]
            cnt = conn2.execute(f'SELECT COUNT(*) as c FROM "{table_name}"').fetchone()["c"]
            print(f"- {table_name}: {cnt}")
    finally:
        conn2.close()


if __name__ == "__main__":
    load_all_tables_to_sqlite()

