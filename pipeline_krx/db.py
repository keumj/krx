from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


DEFAULT_SHARED_DB_ROOT = Path(os.getenv("KEUMJ_KRX_DB_DIR", "data/krx_shared_db"))
DEFAULT_SQLITE_NAME = str(os.getenv("KEUMJ_KRX_DB_SQLITE_NAME", "krx_shared_prices.sqlite")).strip() or "krx_shared_prices.sqlite"

NEWS_ANALYSIS_STATUS_PENDING = "pending"
NEWS_ANALYSIS_STATUS_PROCESSING = "processing"
NEWS_ANALYSIS_STATUS_DONE = "done"
NEWS_ANALYSIS_STATUS_FAILED = "failed"


@dataclass(frozen=True)
class KRXDbInitResult:
    db_path: Path
    project_root: Path
    created: bool


def _normalize_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    if text.isdigit():
        return text.zfill(6)
    return text


def _normalize_text(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    return text


def _normalize_date_text(value: object) -> str | None:
    text = _normalize_text(value)
    if text is None:
        return None
    try:
        return pd.Timestamp(text).normalize().strftime("%Y-%m-%d")
    except Exception:
        return text


def _normalize_number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"nan", "none", "null", "n/a", "-"}:
        return None
    try:
        return float(text)
    except Exception:
        return None


def _resolve_shared_root(shared_db_root: Path | str | None = None) -> Path:
    if shared_db_root is None:
        explicit_root = str(os.getenv("KEUMJ_KRX_DB_DIR", "")).strip()
        if explicit_root:
            return Path(explicit_root)
        return DEFAULT_SHARED_DB_ROOT
    return Path(shared_db_root)


def krx_prices_sqlite_path(shared_db_root: Path | str | None = None) -> Path:
    explicit = str(os.getenv("KEUMJ_KRX_DB_SQLITE_PATH", "")).strip()
    if explicit:
        return Path(explicit)
    return _resolve_shared_root(shared_db_root) / DEFAULT_SQLITE_NAME


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS project_meta (
            key TEXT PRIMARY KEY,
            value_text TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS securities (
            symbol TEXT PRIMARY KEY,
            market TEXT NOT NULL DEFAULT 'UNKNOWN',
            name_kr TEXT,
            name_en TEXT,
            sector TEXT,
            industry TEXT,
            corp_code TEXT,
            isin TEXT,
            listing_date TEXT,
            delisted_date TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            reference_source TEXT NOT NULL DEFAULT 'unknown',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS prices (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL DEFAULT 0,
            trading_value REAL,
            market_cap REAL,
            shares_outstanding REAL,
            foreign_ownership_pct REAL,
            adj_close REAL,
            dividends REAL NOT NULL DEFAULT 0,
            stock_splits REAL NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'KRW',
            source TEXT NOT NULL DEFAULT 'unknown',
            PRIMARY KEY (symbol, date)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fundamentals_snapshot (
            symbol TEXT PRIMARY KEY,
            as_of_date TEXT NOT NULL,
            market TEXT,
            per REAL,
            pbr REAL,
            roe REAL,
            eps REAL,
            bps REAL,
            dividend_yield REAL,
            shares_outstanding REAL,
            market_cap REAL,
            source TEXT NOT NULL DEFAULT 'unknown',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fundamentals_quarterly (
            symbol TEXT NOT NULL,
            fiscal_date TEXT NOT NULL,
            filing_date TEXT,
            period_type TEXT NOT NULL DEFAULT 'quarterly',
            revenue REAL,
            operating_income REAL,
            net_income REAL,
            total_assets REAL,
            total_liabilities REAL,
            stockholders_equity REAL,
            current_assets REAL,
            current_liabilities REAL,
            total_debt REAL,
            operating_cash_flow REAL,
            free_cash_flow REAL,
            capex REAL,
            diluted_eps REAL,
            source TEXT NOT NULL DEFAULT 'unknown',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (symbol, fiscal_date, period_type)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news_articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            publish_date DATETIME NOT NULL,
            title TEXT NOT NULL,
            summary TEXT,
            link TEXT NOT NULL,
            source TEXT NOT NULL,
            language TEXT NOT NULL DEFAULT 'ko',
            provider_query TEXT,
            sentiment_score REAL,
            analysis_status TEXT NOT NULL DEFAULT 'pending'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ingestion_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT,
            status TEXT NOT NULL,
            rows_inserted INTEGER NOT NULL DEFAULT 0,
            rows_updated INTEGER NOT NULL DEFAULT 0,
            rows_skipped INTEGER NOT NULL DEFAULT 0,
            error_text TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS benchmark_definitions (
            benchmark_code TEXT PRIMARY KEY,
            benchmark_name TEXT NOT NULL,
            index_ticker TEXT,
            weighting_method TEXT NOT NULL DEFAULT 'market_cap_proxy',
            latest_as_of_date TEXT,
            source TEXT NOT NULL DEFAULT 'unknown',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS benchmark_constituents (
            benchmark_code TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            benchmark_weight REAL NOT NULL,
            member_order INTEGER,
            market_cap REAL,
            source TEXT NOT NULL DEFAULT 'unknown',
            notes TEXT,
            PRIMARY KEY (benchmark_code, as_of_date, symbol)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS index_constituent_history (
            index_code TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            member_order INTEGER,
            source TEXT NOT NULL DEFAULT 'unknown',
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (index_code, as_of_date, symbol)
        )
        """
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_securities_market_symbol ON securities(market, symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_securities_sector ON securities(sector)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prices_date_symbol ON prices(date, symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prices_symbol_date_market_cap ON prices(symbol, date, market_cap)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fund_snapshot_as_of_date ON fundamentals_snapshot(as_of_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fund_quarterly_symbol_date ON fundamentals_quarterly(symbol, fiscal_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fund_quarterly_fiscal_date ON fundamentals_quarterly(fiscal_date)")
    for column_name in ("current_assets", "current_liabilities", "total_debt"):
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(fundamentals_quarterly)")}
        if column_name not in existing_cols:
            conn.execute(f"ALTER TABLE fundamentals_quarterly ADD COLUMN {column_name} REAL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_symbol_publish_date ON news_articles(symbol, publish_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_publish_date ON news_articles(publish_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_analysis_status_publish_date ON news_articles(analysis_status, publish_date)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_news_symbol_link_unique ON news_articles(symbol, link)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ingestion_runs_dataset_started_at ON ingestion_runs(dataset, started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_benchmark_constituents_code_date ON benchmark_constituents(benchmark_code, as_of_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_benchmark_constituents_symbol ON benchmark_constituents(symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_index_constituent_history_code_date ON index_constituent_history(index_code, as_of_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_index_constituent_history_symbol ON index_constituent_history(symbol)")

    conn.execute("DROP VIEW IF EXISTS news_articles_price_context")
    conn.execute(
        """
        CREATE VIEW IF NOT EXISTS news_articles_price_context AS
        SELECT
            n.id,
            n.symbol,
            n.symbol AS ticker,
            n.publish_date,
            date(n.publish_date) AS publish_day,
            n.title,
            n.summary,
            n.link,
            n.source,
            n.language,
            n.provider_query,
            n.sentiment_score,
            n.analysis_status,
            p.date AS price_date,
            p.open,
            p.high,
            p.low,
            p.close,
            p.adj_close,
            p.volume,
            p.trading_value,
            p.market_cap,
            p.shares_outstanding,
            p.foreign_ownership_pct,
            p.currency
        FROM news_articles AS n
        LEFT JOIN prices AS p
            ON p.symbol = n.symbol
           AND p.date = date(n.publish_date)
        """
    )
    conn.execute("DROP VIEW IF EXISTS news_articles_market_context")
    conn.execute(
        """
        CREATE VIEW IF NOT EXISTS news_articles_market_context AS
        SELECT
            n.id,
            n.symbol,
            n.symbol AS ticker,
            n.publish_date,
            date(n.publish_date) AS publish_day,
            n.title,
            n.summary,
            n.link,
            n.source,
            n.language,
            n.provider_query,
            n.sentiment_score,
            n.analysis_status,
            p.date AS reference_price_date,
            CASE
                WHEN p.date = date(n.publish_date) THEN 1
                ELSE 0
            END AS matched_on_publish_day,
            p.open,
            p.high,
            p.low,
            p.close,
            p.adj_close,
            p.volume,
            p.trading_value,
            p.market_cap,
            p.shares_outstanding,
            p.foreign_ownership_pct,
            p.currency
        FROM news_articles AS n
        LEFT JOIN prices AS p
            ON p.symbol = n.symbol
           AND p.date = (
                SELECT MAX(p2.date)
                FROM prices AS p2
                WHERE p2.symbol = n.symbol
                  AND p2.date <= date(n.publish_date)
           )
        """
    )


def _seed_project_meta(conn: sqlite3.Connection) -> None:
    defaults = {
        "project_code": "KRX",
        "project_name": "Keumj KRX Project",
        "default_currency": "KRW",
        "default_benchmark_symbol": "KS11",
        "minimum_history_start": "2019-12-31",
        "recommended_history_start": "2016-01-01",
    }
    conn.executemany(
        """
        INSERT INTO project_meta(key, value_text)
        VALUES (?, ?)
        ON CONFLICT(key) DO NOTHING
        """,
        list(defaults.items()),
    )


def upsert_krx_securities(
    frame: pd.DataFrame,
    *,
    db_path: Path | str | None = None,
    shared_db_root: Path | str | None = None,
) -> int:
    if frame is None or frame.empty:
        return 0

    target = Path(db_path) if db_path is not None else krx_prices_sqlite_path(shared_db_root)
    target.parent.mkdir(parents=True, exist_ok=True)

    normalized = frame.copy()
    cols = {str(col).strip().lower(): col for col in normalized.columns}
    symbol_col = cols.get("symbol")
    if symbol_col is None:
        raise ValueError("securities frame must include Symbol column")

    rows: list[tuple[object, ...]] = []
    for record in normalized.to_dict(orient="records"):
        symbol = _normalize_symbol(record.get(symbol_col))
        if not symbol:
            continue
        rows.append(
            (
                symbol,
                _normalize_text(record.get(cols.get("market"))) or "UNKNOWN",
                _normalize_text(record.get(cols.get("name_kr")) or record.get(cols.get("name"))),
                _normalize_text(record.get(cols.get("name_en"))),
                _normalize_text(record.get(cols.get("sector"))),
                _normalize_text(record.get(cols.get("industry"))),
                _normalize_text(record.get(cols.get("corp_code"))),
                _normalize_text(record.get(cols.get("isin"))),
                _normalize_date_text(record.get(cols.get("listing_date"))),
                _normalize_date_text(record.get(cols.get("delisted_date"))),
                int(_normalize_number(record.get(cols.get("is_active"))) or 1),
                _normalize_text(record.get(cols.get("reference_source")) or record.get(cols.get("source"))) or "unknown",
            )
        )

    if not rows:
        return 0

    with _connect(target) as conn:
        _ensure_schema(conn)
        before = conn.total_changes
        conn.executemany(
            """
            INSERT INTO securities(
                symbol, market, name_kr, name_en, sector, industry, corp_code, isin,
                listing_date, delisted_date, is_active, reference_source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                market=CASE
                    WHEN excluded.market = 'UNKNOWN' AND securities.market IS NOT NULL THEN securities.market
                    ELSE excluded.market
                END,
                name_kr=COALESCE(excluded.name_kr, securities.name_kr),
                name_en=COALESCE(excluded.name_en, securities.name_en),
                sector=COALESCE(excluded.sector, securities.sector),
                industry=COALESCE(excluded.industry, securities.industry),
                corp_code=COALESCE(excluded.corp_code, securities.corp_code),
                isin=COALESCE(excluded.isin, securities.isin),
                listing_date=COALESCE(excluded.listing_date, securities.listing_date),
                delisted_date=CASE
                    WHEN excluded.is_active = 1 THEN NULL
                    ELSE COALESCE(excluded.delisted_date, securities.delisted_date)
                END,
                is_active=excluded.is_active,
                reference_source=excluded.reference_source,
                updated_at=CURRENT_TIMESTAMP
            """,
            rows,
        )
        conn.commit()
        return int(conn.total_changes - before)


def sync_krx_security_snapshot(
    frame: pd.DataFrame,
    *,
    as_of_date: str | None = None,
    db_path: Path | str | None = None,
    shared_db_root: Path | str | None = None,
) -> tuple[int, int]:
    if frame is None or frame.empty:
        return 0, 0

    snapshot_date = _normalize_date_text(as_of_date) or pd.Timestamp.today().normalize().strftime("%Y-%m-%d")
    normalized = frame.copy()
    cols = {str(col).strip().lower(): col for col in normalized.columns}
    symbol_col = cols.get("symbol")
    if symbol_col is None:
        raise ValueError("snapshot frame must include Symbol column")

    if cols.get("is_active") is None:
        normalized["is_active"] = 1
    else:
        normalized[cols["is_active"]] = 1
    if cols.get("delisted_date") is None:
        normalized["delisted_date"] = None
    else:
        normalized[cols["delisted_date"]] = None

    upserted = upsert_krx_securities(
        normalized,
        db_path=db_path,
        shared_db_root=shared_db_root,
    )

    target = Path(db_path) if db_path is not None else krx_prices_sqlite_path(shared_db_root)
    active_symbols = sorted(
        {
            _normalize_symbol(record.get(symbol_col))
            for record in normalized.to_dict(orient="records")
            if _normalize_symbol(record.get(symbol_col))
        }
    )
    markets = sorted(
        {
            _normalize_text(record.get(cols.get("market"))) or "UNKNOWN"
            for record in normalized.to_dict(orient="records")
            if (_normalize_text(record.get(cols.get("market"))) or "UNKNOWN") != "UNKNOWN"
        }
    )
    if not active_symbols or not markets:
        return upserted, 0

    with _connect(target) as conn:
        _ensure_schema(conn)
        before = conn.total_changes
        market_placeholders = ",".join("?" for _ in markets)
        symbol_placeholders = ",".join("?" for _ in active_symbols)
        params: list[object] = [snapshot_date, *markets, *active_symbols]
        conn.execute(
            f"""
            UPDATE securities
            SET
                is_active = 0,
                delisted_date = COALESCE(delisted_date, ?),
                updated_at = CURRENT_TIMESTAMP
            WHERE market IN ({market_placeholders})
              AND is_active = 1
              AND symbol NOT IN ({symbol_placeholders})
            """,
            params,
        )
        conn.commit()
        deactivated = int(conn.total_changes - before)
    return upserted, deactivated


def upsert_krx_prices(
    frame: pd.DataFrame,
    *,
    db_path: Path | str | None = None,
    shared_db_root: Path | str | None = None,
) -> int:
    if frame is None or frame.empty:
        return 0

    target = Path(db_path) if db_path is not None else krx_prices_sqlite_path(shared_db_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    normalized = frame.copy()
    cols = {str(col).strip().lower(): col for col in normalized.columns}
    required = {"symbol", "date", "open", "high", "low", "close"}
    if not required.issubset(set(cols)):
        raise ValueError("prices frame must include symbol/date/open/high/low/close columns")

    rows: list[tuple[object, ...]] = []
    for record in normalized.to_dict(orient="records"):
        symbol = _normalize_symbol(record.get(cols["symbol"]))
        date_text = _normalize_date_text(record.get(cols["date"]))
        if not symbol or date_text is None:
            continue
        rows.append(
            (
                symbol,
                date_text,
                _normalize_number(record.get(cols["open"])) or 0.0,
                _normalize_number(record.get(cols["high"])) or 0.0,
                _normalize_number(record.get(cols["low"])) or 0.0,
                _normalize_number(record.get(cols["close"])) or 0.0,
                _normalize_number(record.get(cols.get("volume"))) or 0.0,
                _normalize_number(record.get(cols.get("trading_value"))),
                _normalize_number(record.get(cols.get("market_cap"))),
                _normalize_number(record.get(cols.get("shares_outstanding"))),
                _normalize_number(record.get(cols.get("foreign_ownership_pct"))),
                _normalize_number(record.get(cols.get("adj_close"))),
                _normalize_number(record.get(cols.get("dividends"))) or 0.0,
                _normalize_number(record.get(cols.get("stock_splits"))) or 0.0,
                _normalize_text(record.get(cols.get("currency"))) or "KRW",
                _normalize_text(record.get(cols.get("source"))) or "unknown",
            )
        )

    if not rows:
        return 0

    with _connect(target) as conn:
        _ensure_schema(conn)
        before = conn.total_changes
        conn.executemany(
            """
            INSERT INTO prices(
                symbol, date, open, high, low, close, volume, trading_value, market_cap,
                shares_outstanding, foreign_ownership_pct, adj_close, dividends, stock_splits, currency, source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, date) DO UPDATE SET
                open=excluded.open,
                high=excluded.high,
                low=excluded.low,
                close=excluded.close,
                volume=excluded.volume,
                trading_value=excluded.trading_value,
                market_cap=excluded.market_cap,
                shares_outstanding=excluded.shares_outstanding,
                foreign_ownership_pct=excluded.foreign_ownership_pct,
                adj_close=COALESCE(excluded.adj_close, prices.adj_close),
                dividends=excluded.dividends,
                stock_splits=excluded.stock_splits,
                currency=excluded.currency,
                source=excluded.source
            """,
            rows,
        )
        conn.commit()
        return int(conn.total_changes - before)


def upsert_krx_quarterly_fundamentals(
    frame: pd.DataFrame,
    *,
    db_path: Path | str | None = None,
    shared_db_root: Path | str | None = None,
) -> int:
    if frame is None or frame.empty:
        return 0

    target = Path(db_path) if db_path is not None else krx_prices_sqlite_path(shared_db_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    normalized = frame.copy()
    cols = {str(col).strip().lower(): col for col in normalized.columns}
    required = {"symbol", "fiscal_date"}
    if not required.issubset(set(cols)):
        raise ValueError("quarterly fundamentals frame must include symbol and fiscal_date columns")

    rows: list[tuple[object, ...]] = []
    for record in normalized.to_dict(orient="records"):
        symbol = _normalize_symbol(record.get(cols["symbol"]))
        fiscal_date = _normalize_date_text(record.get(cols["fiscal_date"]))
        if not symbol or fiscal_date is None:
            continue
        rows.append(
            (
                symbol,
                fiscal_date,
                _normalize_date_text(record.get(cols.get("filing_date"))),
                _normalize_text(record.get(cols.get("period_type"))) or "quarterly",
                _normalize_number(record.get(cols.get("revenue"))),
                _normalize_number(record.get(cols.get("operating_income"))),
                _normalize_number(record.get(cols.get("net_income"))),
                _normalize_number(record.get(cols.get("total_assets"))),
                _normalize_number(record.get(cols.get("total_liabilities"))),
                _normalize_number(record.get(cols.get("stockholders_equity"))),
                _normalize_number(record.get(cols.get("current_assets"))),
                _normalize_number(record.get(cols.get("current_liabilities"))),
                _normalize_number(record.get(cols.get("total_debt"))),
                _normalize_number(record.get(cols.get("operating_cash_flow"))),
                _normalize_number(record.get(cols.get("free_cash_flow"))),
                _normalize_number(record.get(cols.get("capex"))),
                _normalize_number(record.get(cols.get("diluted_eps"))),
                _normalize_text(record.get(cols.get("source"))) or "unknown",
            )
        )

    if not rows:
        return 0

    with _connect(target) as conn:
        _ensure_schema(conn)
        before = conn.total_changes
        conn.executemany(
            """
            INSERT INTO fundamentals_quarterly(
                symbol, fiscal_date, filing_date, period_type, revenue, operating_income, net_income,
                total_assets, total_liabilities, stockholders_equity, current_assets, current_liabilities,
                total_debt, operating_cash_flow, free_cash_flow, capex, diluted_eps, source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, fiscal_date, period_type) DO UPDATE SET
                filing_date=COALESCE(excluded.filing_date, fundamentals_quarterly.filing_date),
                revenue=excluded.revenue,
                operating_income=excluded.operating_income,
                net_income=excluded.net_income,
                total_assets=excluded.total_assets,
                total_liabilities=excluded.total_liabilities,
                stockholders_equity=excluded.stockholders_equity,
                current_assets=excluded.current_assets,
                current_liabilities=excluded.current_liabilities,
                total_debt=excluded.total_debt,
                operating_cash_flow=excluded.operating_cash_flow,
                free_cash_flow=excluded.free_cash_flow,
                capex=excluded.capex,
                diluted_eps=excluded.diluted_eps,
                source=excluded.source,
                updated_at=CURRENT_TIMESTAMP
            """,
            rows,
        )
        conn.commit()
        return int(conn.total_changes - before)


def upsert_krx_benchmark_snapshot(
    frame: pd.DataFrame,
    *,
    benchmark_code: str,
    benchmark_name: str,
    as_of_date: str,
    weighting_method: str = "market_cap_proxy",
    index_ticker: str | None = None,
    source: str = "unknown",
    db_path: Path | str | None = None,
    shared_db_root: Path | str | None = None,
) -> int:
    if frame is None or frame.empty:
        return 0

    target = Path(db_path) if db_path is not None else krx_prices_sqlite_path(shared_db_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    normalized = frame.copy()
    cols = {str(col).strip().lower(): col for col in normalized.columns}
    symbol_col = cols.get("symbol")
    if symbol_col is None:
        raise ValueError("benchmark frame must include symbol column")

    if cols.get("benchmark_weight") is None:
        raise ValueError("benchmark frame must include benchmark_weight column")

    snapshot_date = _normalize_date_text(as_of_date)
    if snapshot_date is None:
        raise ValueError("as_of_date is required for benchmark snapshot")

    rows: list[tuple[object, ...]] = []
    order_counter = 0
    for record in normalized.to_dict(orient="records"):
        symbol = _normalize_symbol(record.get(symbol_col))
        weight = _normalize_number(record.get(cols.get("benchmark_weight")))
        if not symbol or weight is None:
            continue
        order_counter += 1
        rows.append(
            (
                _normalize_text(benchmark_code) or benchmark_code,
                snapshot_date,
                symbol,
                float(weight),
                int(_normalize_number(record.get(cols.get("member_order"))) or order_counter),
                _normalize_number(record.get(cols.get("market_cap"))),
                _normalize_text(record.get(cols.get("source"))) or source,
                _normalize_text(record.get(cols.get("notes"))),
            )
        )

    if not rows:
        return 0

    with _connect(target) as conn:
        _ensure_schema(conn)
        before = conn.total_changes
        conn.execute(
            """
            INSERT INTO benchmark_definitions(
                benchmark_code, benchmark_name, index_ticker, weighting_method, latest_as_of_date, source
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(benchmark_code) DO UPDATE SET
                benchmark_name=excluded.benchmark_name,
                index_ticker=COALESCE(excluded.index_ticker, benchmark_definitions.index_ticker),
                weighting_method=excluded.weighting_method,
                latest_as_of_date=excluded.latest_as_of_date,
                source=excluded.source,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                _normalize_text(benchmark_code) or benchmark_code,
                _normalize_text(benchmark_name) or benchmark_name,
                _normalize_text(index_ticker),
                _normalize_text(weighting_method) or "market_cap_proxy",
                snapshot_date,
                _normalize_text(source) or "unknown",
            ),
        )
        conn.executemany(
            """
            INSERT INTO benchmark_constituents(
                benchmark_code, as_of_date, symbol, benchmark_weight, member_order, market_cap, source, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(benchmark_code, as_of_date, symbol) DO UPDATE SET
                benchmark_weight=excluded.benchmark_weight,
                member_order=excluded.member_order,
                market_cap=excluded.market_cap,
                source=excluded.source,
                notes=COALESCE(excluded.notes, benchmark_constituents.notes)
            """,
            rows,
        )
        conn.commit()
        return int(conn.total_changes - before)


def upsert_index_constituent_history(
    frame: pd.DataFrame,
    *,
    index_code: str,
    as_of_date: str,
    source: str = "unknown",
    db_path: Path | str | None = None,
    shared_db_root: Path | str | None = None,
) -> int:
    if frame is None or frame.empty:
        return 0

    target = Path(db_path) if db_path is not None else krx_prices_sqlite_path(shared_db_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    normalized = frame.copy()
    cols = {str(col).strip().lower(): col for col in normalized.columns}
    symbol_col = cols.get("symbol")
    if symbol_col is None:
        raise ValueError("index constituent frame must include symbol column")

    snapshot_date = _normalize_date_text(as_of_date)
    if snapshot_date is None:
        raise ValueError("as_of_date is required for index constituent history")

    rows: list[tuple[object, ...]] = []
    order_counter = 0
    for record in normalized.to_dict(orient="records"):
        symbol = _normalize_symbol(record.get(symbol_col))
        if not symbol:
            continue
        order_counter += 1
        rows.append(
            (
                _normalize_text(index_code) or index_code,
                snapshot_date,
                symbol,
                int(_normalize_number(record.get(cols.get("member_order"))) or order_counter),
                _normalize_text(record.get(cols.get("source"))) or source,
                _normalize_text(record.get(cols.get("notes"))),
            )
        )

    if not rows:
        return 0

    with _connect(target) as conn:
        _ensure_schema(conn)
        before = conn.total_changes
        conn.executemany(
            """
            INSERT INTO index_constituent_history(
                index_code, as_of_date, symbol, member_order, source, notes
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(index_code, as_of_date, symbol) DO UPDATE SET
                member_order=excluded.member_order,
                source=excluded.source,
                notes=COALESCE(excluded.notes, index_constituent_history.notes)
            """,
            rows,
        )
        conn.commit()
        return int(conn.total_changes - before)


def load_latest_index_constituent_history(
    index_code: str,
    *,
    db_path: Path | str | None = None,
    shared_db_root: Path | str | None = None,
) -> pd.DataFrame:
    target = Path(db_path) if db_path is not None else krx_prices_sqlite_path(shared_db_root)
    if not target.exists() or not target.is_file():
        return pd.DataFrame()

    with _connect(target) as conn:
        _ensure_schema(conn)
        latest_row = conn.execute(
            """
            SELECT MAX(as_of_date)
            FROM index_constituent_history
            WHERE index_code = ?
            """,
            (_normalize_text(index_code) or index_code,),
        ).fetchone()
        if latest_row is None or latest_row[0] is None:
            return pd.DataFrame()
        latest_as_of_date = str(latest_row[0])
        return pd.read_sql_query(
            """
            SELECT
                h.index_code,
                h.as_of_date,
                h.symbol,
                s.market,
                s.name_kr,
                s.sector,
                s.industry,
                h.member_order,
                h.source,
                h.notes
            FROM index_constituent_history AS h
            LEFT JOIN securities AS s
                ON s.symbol = h.symbol
            WHERE h.index_code = ?
              AND h.as_of_date = ?
            ORDER BY h.member_order ASC, h.symbol ASC
            """,
            conn,
            params=[_normalize_text(index_code) or index_code, latest_as_of_date],
        )


def load_latest_krx_benchmark_snapshot(
    benchmark_code: str,
    *,
    db_path: Path | str | None = None,
    shared_db_root: Path | str | None = None,
) -> pd.DataFrame:
    target = Path(db_path) if db_path is not None else krx_prices_sqlite_path(shared_db_root)
    if not target.exists() or not target.is_file():
        return pd.DataFrame()

    with _connect(target) as conn:
        _ensure_schema(conn)
        latest_row = conn.execute(
            """
            SELECT latest_as_of_date
            FROM benchmark_definitions
            WHERE benchmark_code = ?
            """,
            (_normalize_text(benchmark_code) or benchmark_code,),
        ).fetchone()
        if latest_row is None or latest_row[0] is None:
            return pd.DataFrame()
        latest_as_of_date = str(latest_row[0])
        return pd.read_sql_query(
            """
            SELECT
                c.benchmark_code,
                c.as_of_date,
                c.symbol,
                s.market,
                s.name_kr,
                s.sector,
                s.industry,
                c.benchmark_weight,
                c.member_order,
                c.market_cap,
                c.source,
                c.notes
            FROM benchmark_constituents AS c
            LEFT JOIN securities AS s
                ON s.symbol = c.symbol
            WHERE c.benchmark_code = ?
              AND c.as_of_date = ?
            ORDER BY c.member_order ASC, c.symbol ASC
            """,
            conn,
            params=[_normalize_text(benchmark_code) or benchmark_code, latest_as_of_date],
        )


def init_krx_project_db(
    shared_db_root: Path | str | None = None,
    *,
    db_path: Path | str | None = None,
) -> KRXDbInitResult:
    project_root = _resolve_shared_root(shared_db_root)
    target = Path(db_path) if db_path is not None else krx_prices_sqlite_path(project_root)
    created = not target.exists()
    target.parent.mkdir(parents=True, exist_ok=True)

    with _connect(target) as conn:
        _ensure_schema(conn)
        _seed_project_meta(conn)
        conn.commit()

    return KRXDbInitResult(
        db_path=target,
        project_root=project_root,
        created=created,
    )


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Initialize the separate KRX project SQLite database.")
    parser.add_argument("--shared-db-root", default=str(DEFAULT_SHARED_DB_ROOT), help="KRX project DB root directory")
    parser.add_argument("--db-path", default="", help="Optional SQLite file path override")
    args = parser.parse_args()

    result = init_krx_project_db(
        args.shared_db_root,
        db_path=Path(args.db_path) if str(args.db_path).strip() else None,
    )
    print(
        f"Initialized KRX SQLite DB: {result.db_path} "
        f"(project_root={result.project_root}, created={result.created})"
    )
    return 0
