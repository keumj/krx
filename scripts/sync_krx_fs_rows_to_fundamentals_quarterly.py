from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline_krx.db import init_krx_project_db, upsert_krx_quarterly_fundamentals
from scripts.load_krx_fs_to_shared_db import DEFAULT_DB_PATH, DEFAULT_TABLE


@dataclass(frozen=True)
class KRXFsFundamentalsSyncResult:
    source_reports: int
    transformed_rows: int
    changed_rows: int
    db_path: Path


ACCOUNT_ID_CANDIDATES = {
    "revenue": (
        "ifrs-full_Revenue",
        "ifrs-full_RevenueFromContractsWithCustomers",
        "dart_Revenue",
    ),
    "operating_income": (
        "dart_OperatingIncomeLoss",
        "ifrs-full_ProfitLossFromOperatingActivities",
    ),
    "net_income": (
        "ifrs-full_ProfitLoss",
        "dart_NetIncomeLoss",
    ),
    "total_assets": (
        "ifrs-full_Assets",
        "dart_AssetsTotal",
    ),
    "total_liabilities": (
        "ifrs-full_Liabilities",
        "dart_LiabilitiesTotal",
    ),
    "stockholders_equity": (
        "ifrs-full_Equity",
        "dart_EquityTotal",
    ),
    "current_assets": (
        "ifrs-full_CurrentAssets",
        "dart_CurrentAssets",
    ),
    "current_liabilities": (
        "ifrs-full_CurrentLiabilities",
        "dart_CurrentLiabilities",
    ),
    "operating_cash_flow": (
        "ifrs-full_CashFlowsFromUsedInOperatingActivities",
        "dart_CashFlowsFromUsedInOperatingActivities",
    ),
    "capex": (
        "ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
        "ifrs-full_PurchaseOfPropertyPlantAndEquipment",
        "dart_PurchaseOfPropertyPlantAndEquipment",
    ),
}

ACCOUNT_NAME_CANDIDATES = {
    "revenue": ("매출액", "영업수익", "수익(매출액)", "수익"),
    "operating_income": ("영업이익", "영업손익"),
    "net_income": ("당기순이익", "분기순이익", "반기순이익", "연결당기순이익"),
    "total_assets": ("자산총계",),
    "total_liabilities": ("부채총계",),
    "stockholders_equity": ("자본총계", "자본총액"),
    "current_assets": ("유동자산",),
    "current_liabilities": ("유동부채",),
    "operating_cash_flow": ("영업활동현금흐름", "영업활동으로 인한 현금흐름"),
    "capex": ("유형자산의 취득", "유형자산 취득"),
}

STATEMENT_KEYWORDS = {
    "balance": ("재무상태", "Balance"),
    "income": ("손익", "포괄손익", "Income", "Profit"),
    "cashflow": ("현금흐름", "Cash"),
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Transform raw krx_fs_rows in shared SQLite into normalized fundamentals_quarterly rows."
    )
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Shared SQLite DB path.")
    parser.add_argument("--raw-table", default=DEFAULT_TABLE, help="Raw KRX FS rows table name.")
    parser.add_argument("--symbol", default="", help="Optional single symbol to transform.")
    parser.add_argument("--limit-reports", type=int, default=0, help="Optional max report files to transform.")
    parser.add_argument("--batch-reports", type=int, default=500, help="Number of report files to transform per SQLite batch.")
    return parser.parse_args()


def _quote_ident(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _normalize_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    return text.zfill(6) if text.isdigit() else text


def _normalize_number(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"nan", "none", "null", "n/a", "-"}:
        return None
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    try:
        numeric = float(text)
    except Exception:
        return None
    if not np.isfinite(numeric):
        return None
    return numeric


def _contains_any(value: object, needles: tuple[str, ...]) -> bool:
    text = str(value or "")
    return any(needle in text for needle in needles)


def _preferred_statement_rows(frame: pd.DataFrame, keywords: tuple[str, ...]) -> pd.DataFrame:
    if "statement_name" not in frame.columns:
        return frame.copy()
    out = frame[frame["statement_name"].map(lambda value: _contains_any(value, keywords))].copy()
    if out.empty:
        return out
    if "consolidation" in out.columns:
        consolidation = out["consolidation"].astype(str)
        if consolidation.str.contains("연결", na=False).any():
            out = out[consolidation.str.contains("연결", na=False)].copy()
    return out


def _pick_amount(frame: pd.DataFrame, metric: str, statement_key: str) -> float | None:
    sub = _preferred_statement_rows(frame, STATEMENT_KEYWORDS[statement_key])
    if sub.empty:
        sub = frame.copy()

    account_ids = ACCOUNT_ID_CANDIDATES.get(metric, ())
    if account_ids and "account_id" in sub.columns:
        id_series = sub["account_id"].astype(str)
        for candidate in account_ids:
            matched = sub[id_series == candidate]
            values = pd.to_numeric(matched.get("amount"), errors="coerce").dropna()
            if not values.empty:
                return _normalize_number(values.iloc[0])

    accounts = sub.get("account_name")
    if accounts is None:
        return None
    account_text = accounts.astype(str)
    for candidate in ACCOUNT_NAME_CANDIDATES.get(metric, ()):
        exact = sub[account_text == candidate]
        values = pd.to_numeric(exact.get("amount"), errors="coerce").dropna()
        if not values.empty:
            return _normalize_number(values.iloc[0])
    for candidate in ACCOUNT_NAME_CANDIDATES.get(metric, ()):
        contains = sub[account_text.str.contains(candidate, regex=False, na=False)]
        values = pd.to_numeric(contains.get("amount"), errors="coerce").dropna()
        if not values.empty:
            return _normalize_number(values.iloc[0])
    return None


def _sum_named_amounts(
    frame: pd.DataFrame,
    *,
    statement_key: str,
    include_keywords: tuple[str, ...],
    exclude_keywords: tuple[str, ...] = (),
) -> float | None:
    sub = _preferred_statement_rows(frame, STATEMENT_KEYWORDS[statement_key])
    if sub.empty or "account_name" not in sub.columns:
        return None
    accounts = sub["account_name"].astype(str)
    mask = pd.Series(False, index=sub.index)
    for keyword in include_keywords:
        mask = mask | accounts.str.contains(keyword, regex=False, na=False)
    for keyword in exclude_keywords:
        mask = mask & ~accounts.str.contains(keyword, regex=False, na=False)
    values = pd.to_numeric(sub.loc[mask, "amount"], errors="coerce").dropna()
    if values.empty:
        return None
    return _normalize_number(values.sum())


def _period_type(report_name: object) -> str:
    text = str(report_name or "").strip().lower()
    if "q1" in text or "11013" in text or "1분기" in text:
        return "q1"
    if "half_year" in text or "half-year" in text or "11012" in text or "반기" in text:
        return "half_year"
    if "q3" in text or "11014" in text or "3분기" in text:
        return "q3"
    if "annual" in text or "11011" in text or "사업보고" in text:
        return "annual"
    return "quarterly"


def _fallback_fiscal_date(report_year: object, period_type: str) -> str | None:
    try:
        year = int(report_year)
    except Exception:
        return None
    suffix = {
        "q1": "03-31",
        "half_year": "06-30",
        "q3": "09-30",
        "annual": "12-31",
    }.get(period_type, "12-31")
    return f"{year:04d}-{suffix}"


def _extract_fiscal_date(values: pd.Series) -> str | None:
    for value in values.dropna().astype(str):
        matches = re.findall(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", value)
        if matches:
            year, month, day = matches[-1]
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return None


def _transform_report(frame: pd.DataFrame) -> dict[str, object] | None:
    frame = frame.copy()
    frame["amount"] = frame["amount"].map(_normalize_number)
    frame = frame.dropna(subset=["amount"])
    if frame.empty:
        return None

    first = frame.iloc[0]
    period_type = _period_type(first.get("report_name"))
    fiscal_date = _extract_fiscal_date(frame.get("period_label", pd.Series(dtype=object)))
    fiscal_date = fiscal_date or _fallback_fiscal_date(first.get("report_year"), period_type)
    if fiscal_date is None:
        return None

    total_assets = _pick_amount(frame, "total_assets", "balance")
    total_liabilities = _pick_amount(frame, "total_liabilities", "balance")
    stockholders_equity = _pick_amount(frame, "stockholders_equity", "balance")
    if stockholders_equity is None and total_assets is not None and total_liabilities is not None:
        stockholders_equity = total_assets - total_liabilities

    total_debt = _sum_named_amounts(
        frame,
        statement_key="balance",
        include_keywords=("차입", "사채"),
        exclude_keywords=("할인발행차금", "전환권조정", "신주인수권조정"),
    )

    operating_cash_flow = _pick_amount(frame, "operating_cash_flow", "cashflow")
    capex = _pick_amount(frame, "capex", "cashflow")
    free_cash_flow = None
    if operating_cash_flow is not None and capex is not None:
        free_cash_flow = operating_cash_flow - abs(capex)

    return {
        "symbol": _normalize_symbol(first.get("symbol")),
        "fiscal_date": fiscal_date,
        "filing_date": first.get("filing_date"),
        "period_type": period_type,
        "revenue": _pick_amount(frame, "revenue", "income"),
        "operating_income": _pick_amount(frame, "operating_income", "income"),
        "net_income": _pick_amount(frame, "net_income", "income"),
        "total_assets": total_assets,
        "total_liabilities": total_liabilities,
        "stockholders_equity": stockholders_equity,
        "current_assets": _pick_amount(frame, "current_assets", "balance"),
        "current_liabilities": _pick_amount(frame, "current_liabilities", "balance"),
        "total_debt": total_debt,
        "operating_cash_flow": operating_cash_flow,
        "free_cash_flow": free_cash_flow,
        "capex": capex,
        "shares_outstanding": None,
        "diluted_eps": None,
        "source": f"krx_fs_rows:{first.get('source_file')}",
    }


def _attach_shares_and_eps(frame: pd.DataFrame, db_path: Path) -> pd.DataFrame:
    if frame.empty or "symbol" not in frame.columns or "fiscal_date" not in frame.columns:
        return frame

    out = frame.copy()
    out["symbol"] = out["symbol"].map(_normalize_symbol)
    out["fiscal_date"] = pd.to_datetime(out["fiscal_date"], errors="coerce")
    symbols = sorted({symbol for symbol in out["symbol"].dropna().tolist() if symbol})
    max_fiscal_date = out["fiscal_date"].dropna().max()
    if not symbols or pd.isna(max_fiscal_date):
        out["fiscal_date"] = out["fiscal_date"].dt.strftime("%Y-%m-%d")
        return out

    placeholders = ",".join("?" for _ in symbols)
    query = (
        "SELECT symbol, date, shares_outstanding "
        "FROM prices "
        f"WHERE symbol IN ({placeholders}) "
        "AND date <= ? "
        "AND shares_outstanding IS NOT NULL "
        "ORDER BY symbol, date"
    )
    params: list[object] = [*symbols, pd.Timestamp(max_fiscal_date).strftime("%Y-%m-%d")]
    with sqlite3.connect(db_path) as conn:
        shares = pd.read_sql_query(query, conn, params=params)
    if shares.empty:
        out["fiscal_date"] = out["fiscal_date"].dt.strftime("%Y-%m-%d")
        return out

    shares["symbol"] = shares["symbol"].map(_normalize_symbol)
    shares["date"] = pd.to_datetime(shares["date"], errors="coerce")
    shares["shares_outstanding"] = pd.to_numeric(shares["shares_outstanding"], errors="coerce")
    shares = shares.dropna(subset=["symbol", "date", "shares_outstanding"]).sort_values(["symbol", "date"])
    if shares.empty:
        out["fiscal_date"] = out["fiscal_date"].dt.strftime("%Y-%m-%d")
        return out

    out["_row_order"] = range(len(out.index))
    merged_parts: list[pd.DataFrame] = []
    for symbol, sub in out.sort_values(["symbol", "fiscal_date"]).groupby("symbol", sort=False):
        share_sub = shares[shares["symbol"] == symbol]
        if share_sub.empty:
            merged_parts.append(sub)
            continue
        merged = pd.merge_asof(
            sub.sort_values("fiscal_date"),
            share_sub[["date", "shares_outstanding"]].sort_values("date"),
            left_on="fiscal_date",
            right_on="date",
            direction="backward",
        ).drop(columns=["date"])
        merged_parts.append(merged)

    out = pd.concat(merged_parts, axis=0, ignore_index=True).sort_values("_row_order").drop(columns=["_row_order"])
    if "shares_outstanding_x" in out.columns or "shares_outstanding_y" in out.columns:
        base = pd.to_numeric(out.get("shares_outstanding_x"), errors="coerce")
        loaded = pd.to_numeric(out.get("shares_outstanding_y"), errors="coerce")
        out["shares_outstanding"] = base.combine_first(loaded)
        out = out.drop(columns=[col for col in ["shares_outstanding_x", "shares_outstanding_y"] if col in out.columns])

    for column in ("shares_outstanding", "net_income", "diluted_eps"):
        if column not in out.columns:
            out[column] = np.nan

    shares_outstanding = pd.to_numeric(out.get("shares_outstanding"), errors="coerce")
    net_income = pd.to_numeric(out.get("net_income"), errors="coerce")
    existing_eps = pd.to_numeric(out.get("diluted_eps"), errors="coerce")
    calculated_eps = net_income / shares_outstanding.where(shares_outstanding > 0.0)
    out["diluted_eps"] = existing_eps.combine_first(calculated_eps)
    out["fiscal_date"] = out["fiscal_date"].dt.strftime("%Y-%m-%d")
    return out


def _list_source_files(db_path: Path, raw_table: str, symbol: str | None, limit_reports: int | None) -> list[str]:
    params: list[object] = []
    where = ""
    if symbol:
        where = "WHERE symbol = ?"
        params.append(_normalize_symbol(symbol))
    limit_sql = ""
    if limit_reports is not None and int(limit_reports) > 0:
        limit_sql = " LIMIT ?"
        params.append(int(limit_reports))
    with sqlite3.connect(db_path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (raw_table,),
        ).fetchone()
        if exists is None:
            raise RuntimeError(f"Raw table not found: {raw_table}")
        rows = conn.execute(
            f"""
            SELECT source_file
            FROM {_quote_ident(raw_table)}
            {where}
            GROUP BY source_file
            ORDER BY MAX(filing_date) DESC, source_file DESC
            {limit_sql}
            """,
            params,
        ).fetchall()
    return [str(row[0]) for row in rows]


def _load_raw_rows_for_source_files(db_path: Path, raw_table: str, source_files: list[str]) -> pd.DataFrame:
    if not source_files:
        return pd.DataFrame()
    placeholders = ",".join("?" for _ in source_files)
    query = (
        f"SELECT * FROM {_quote_ident(raw_table)} "
        f"WHERE source_file IN ({placeholders}) "
        "ORDER BY symbol, source_file, source_row_number"
    )
    with sqlite3.connect(db_path) as conn:
        return pd.read_sql_query(query, conn, params=source_files)


def _normalize_raw_columns(frame: pd.DataFrame) -> pd.DataFrame:
    rename_map = {
        "개별/연결": "consolidation",
        "계정명": "account_name",
        "당기일자": "period_label",
        "금액": "amount",
        "재무제표명": "statement_name",
    }
    out = frame.rename(columns={k: v for k, v in rename_map.items() if k in frame.columns}).copy()
    required = [
        "symbol",
        "filing_date",
        "report_year",
        "report_name",
        "source_file",
        "source_row_number",
        "consolidation",
        "account_name",
        "period_label",
        "amount",
        "statement_name",
    ]
    missing = [col for col in required if col not in out.columns]
    if missing:
        raise RuntimeError(f"Raw table is missing required columns: {', '.join(missing)}")
    return out


def sync_krx_fs_rows_to_fundamentals_quarterly(
    *,
    db_path: Path = DEFAULT_DB_PATH,
    raw_table: str = DEFAULT_TABLE,
    symbol: str | None = None,
    limit_reports: int | None = None,
    batch_reports: int = 500,
) -> KRXFsFundamentalsSyncResult:
    init_krx_project_db(db_path=db_path)
    source_files = _list_source_files(db_path, raw_table, symbol, limit_reports)
    if not source_files:
        return KRXFsFundamentalsSyncResult(0, 0, 0, db_path)

    changed = 0
    transformed_rows = 0
    batch_size = max(1, int(batch_reports))
    for start in range(0, len(source_files), batch_size):
        batch_files = source_files[start : start + batch_size]
        raw = _load_raw_rows_for_source_files(db_path, raw_table, batch_files)
        if raw.empty:
            continue
        raw = _normalize_raw_columns(raw)
        rows: list[dict[str, object]] = []
        for _, report in raw.groupby("source_file", sort=False):
            transformed = _transform_report(report)
            if transformed is not None:
                rows.append(transformed)
        if not rows:
            continue
        normalized = pd.DataFrame(rows)
        normalized = normalized.drop_duplicates(subset=["symbol", "fiscal_date", "period_type"], keep="last")
        normalized = _attach_shares_and_eps(normalized, db_path)
        transformed_rows += int(len(normalized.index))
        changed += upsert_krx_quarterly_fundamentals(normalized, db_path=db_path)
        print(
            f"[krx-fs-fundamentals-sync] reports={min(start + batch_size, len(source_files))}/{len(source_files)} "
            f"transformed_rows={transformed_rows} changed_rows={changed}",
            flush=True,
        )
    return KRXFsFundamentalsSyncResult(
        source_reports=int(len(source_files)),
        transformed_rows=int(transformed_rows),
        changed_rows=int(changed),
        db_path=db_path,
    )


def main() -> int:
    args = _parse_args()
    result = sync_krx_fs_rows_to_fundamentals_quarterly(
        db_path=Path(args.db_path),
        raw_table=str(args.raw_table).strip() or DEFAULT_TABLE,
        symbol=str(args.symbol).strip() or None,
        limit_reports=int(args.limit_reports) or None,
        batch_reports=int(args.batch_reports),
    )
    print(
        "KRX FS fundamentals sync complete:",
        f"source_reports={result.source_reports}",
        f"transformed_rows={result.transformed_rows}",
        f"changed_rows={result.changed_rows}",
        f"db={result.db_path}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
