from __future__ import annotations

import os
import sqlite3
import io
import hashlib
import base64
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline_common.notebook_data import load_krx_components
from pipeline_common.shared_fundamentals import derive_shared_fundamental_metrics
from pipeline_common.shared_krx_prices_sql import (
    load_shared_close_prices_for_symbols,
    load_shared_market_caps_for_symbols,
    shared_prices_sqlite_path,
)
from pipeline_krx_stock_news.analysis import heuristic_title_sentiment

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.font_manager as fm
from matplotlib import colors as mcolors
from matplotlib.patches import Patch
from pipeline_krx.db import load_latest_index_constituent_history, load_latest_krx_benchmark_snapshot

DEFAULT_PORTFOLIO_DB_ROOT = Path(os.getenv("KEUMJ_PORTFOLIO_DB_DIR", "data/portfolio"))
DEFAULT_PORTFOLIO_DB_NAME = str(os.getenv("KEUMJ_PORTFOLIO_DB_NAME", "portfolio.sqlite")).strip() or "portfolio.sqlite"
DEFAULT_LOOKBACK_DAYS = 252
DEFAULT_VIRTUAL_FORECAST_HORIZON_DAYS = 10
DEFAULT_OPTIMIZATION_UNIVERSE_SIZE = 120
DEFAULT_TOP_HOLDINGS = 20
DEFAULT_SECTOR_CAP_PCT = 30.0
DEFAULT_MAX_POSITION_PCT = 8.0
DEFAULT_CASH_BUFFER_PCT = 5.0
DEFAULT_BENCHMARK_CODE = "KOSPI200"


def _configure_chart_fonts() -> None:
    preferred = ["Malgun Gothic", "Noto Sans KR", "NanumGothic", "AppleGothic"]
    available = {font.name for font in fm.fontManager.ttflist}
    for name in preferred:
        if name in available:
            plt.rcParams["font.family"] = name
            break
    plt.rcParams["axes.unicode_minus"] = False


_configure_chart_fonts()

# 고정된 섹터별 색상 팔레트 (Portfolio와 KRX 차트 간 일관성 유지)
SECTOR_COLOR_PALETTE = {
    "Information Technology": "#3498db",  # Blue
    "Health Care": "#e74c3c",             # Red
    "Financials": "#f1c40f",              # Yellow
    "Consumer Discretionary": "#e67e22",   # Orange
    "Communication Services": "#9b59b6",   # Purple
    "Industrials": "#95a5a6",              # Gray
    "Consumer Staples": "#1abc9c",         # Turquoise
    "Energy": "#34495e",                   # Dark Blue/Gray
    "Utilities": "#2ecc71",                # Emerald
    "Real Estate": "#d35400",              # Pumpkin
    "Materials": "#7f8c8d",                # Concrete
    "Cash": "#27ae60",                     # Green
    "Unknown": "#bdc3c7"                   # Silver
}


@dataclass(frozen=True)
class PortfolioDashboard:
    as_of_date: str | None
    trades: pd.DataFrame
    positions: pd.DataFrame
    holdings_performance: pd.DataFrame
    portfolio_summary: pd.DataFrame
    attribution: pd.DataFrame
    stock_attribution: pd.DataFrame
    style_attribution: pd.DataFrame
    risk_summary: pd.DataFrame
    relative_risk_summary: pd.DataFrame
    risk_contribution: pd.DataFrame
    active_risk_contribution: pd.DataFrame
    factor_risk: pd.DataFrame
    style_exposure: pd.DataFrame
    scoring: pd.DataFrame
    diagnostics: dict[str, str]
    cumulative_chart: str | None = None
    sector_contribution_chart: str | None = None
    style_exposure_chart: str | None = None
    sector_allocation_chart: str | None = None
    benchmark_sector_allocation_chart: str | None = None
    risk_contribution_chart: str | None = None
    active_risk_contribution_chart: str | None = None
    integrated_score_chart: str | None = None
    best_scoring_stocks: pd.DataFrame | None = None
    worst_scoring_stocks: pd.DataFrame | None = None
    top_recommendations: pd.DataFrame | None = None
    scoring_commentary: str | None = None


@dataclass(frozen=True)
class VirtualTradeResult:
    input_summary: pd.DataFrame
    before_summary: pd.DataFrame
    after_summary: pd.DataFrame
    position_changes: pd.DataFrame
    risk_changes: pd.DataFrame
    diagnostics: dict[str, str]


@dataclass(frozen=True)
class OptimizationResult:
    replication: pd.DataFrame
    aggressive: pd.DataFrame
    defensive: pd.DataFrame
    diagnostics: pd.DataFrame
    replication_chart: str | None = None
    aggressive_chart: str | None = None
    defensive_chart: str | None = None
    impact_summary: pd.DataFrame | None = None


def portfolio_db_path(db_path: Path | str | None = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    explicit = str(os.getenv("KEUMJ_PORTFOLIO_DB_PATH", "")).strip()
    if explicit:
        return Path(explicit)
    return DEFAULT_PORTFOLIO_DB_ROOT / DEFAULT_PORTFOLIO_DB_NAME


def _get_db_max_date(shared_db: Path | str | None = None) -> pd.Timestamp:
    """데이터베이스에서 실제 가격 데이터가 있는 가장 최신 날짜를 조회합니다 (Read-only)."""
    target = _shared_db_path(shared_db)
    if not target.exists():
        return pd.Timestamp.today().normalize()
    try:
        with sqlite3.connect(target) as conn:
            res = conn.execute("SELECT MAX(date) FROM prices").fetchone()
            if res and res[0]:
                return pd.Timestamp(res[0]).normalize()
    except Exception:
        pass
    return pd.Timestamp.today().normalize()


def _shared_db_path(shared_db: Path | str | None = None) -> Path:
    return Path(shared_db) if shared_db is not None else shared_prices_sqlite_path()


def _clean_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    text = "".join(ch for ch in text if ch.isalnum() or ch in {"_", ".", "-"})
    return text.zfill(6) if text.isdigit() else text


def resolve_security_symbol(value: object, *, shared_db: Path | str | None = None) -> str:
    raw = str(value or "").strip()
    symbol_candidate = _clean_symbol(raw)
    if not raw or not symbol_candidate:
        raise ValueError("ticker must not be empty")
    if symbol_candidate == "CASH":
        return "CASH"

    target = _shared_db_path(shared_db)
    if target.exists():
        try:
            with sqlite3.connect(target) as conn:
                row = conn.execute(
                    """
                    SELECT symbol
                    FROM securities
                    WHERE UPPER(symbol) = ?
                       OR UPPER(REPLACE(symbol, ' ', '')) = ?
                    ORDER BY is_active DESC, market ASC, symbol ASC
                    LIMIT 1
                    """,
                    (symbol_candidate, symbol_candidate),
                ).fetchone()
                if row and row[0]:
                    return str(row[0]).strip().upper()

                exact_rows = conn.execute(
                    """
                    SELECT symbol, name_kr, name_en, market, is_active
                    FROM securities
                    WHERE name_kr = ?
                       OR UPPER(name_en) = UPPER(?)
                    ORDER BY is_active DESC, market ASC, symbol ASC
                    LIMIT 5
                    """,
                    (raw, raw),
                ).fetchall()
                if len(exact_rows) == 1:
                    return str(exact_rows[0][0]).strip().upper()
                if len(exact_rows) > 1:
                    matches = ", ".join(f"{row[1] or row[2]}({row[0]})" for row in exact_rows)
                    raise ValueError(f"ambiguous ticker/name '{raw}': {matches}")

                like = f"%{raw}%"
                like_rows = conn.execute(
                    """
                    SELECT symbol, name_kr, name_en, market, is_active
                    FROM securities
                    WHERE name_kr LIKE ?
                       OR UPPER(name_en) LIKE UPPER(?)
                    ORDER BY
                        CASE WHEN name_kr = ? OR UPPER(name_en) = UPPER(?) THEN 0 ELSE 1 END,
                        is_active DESC,
                        market ASC,
                        symbol ASC
                    LIMIT 6
                    """,
                    (like, like, raw, raw),
                ).fetchall()
                if len(like_rows) == 1:
                    return str(like_rows[0][0]).strip().upper()
                if len(like_rows) > 1:
                    matches = ", ".join(f"{row[1] or row[2]}({row[0]})" for row in like_rows[:5])
                    raise ValueError(f"ambiguous ticker/name '{raw}': {matches}")
        except sqlite3.Error:
            pass

    if all(ch.isascii() and (ch.isalnum() or ch in {"_", ".", "-"}) for ch in raw):
        return symbol_candidate
    raise ValueError(f"ticker/name '{raw}' was not found in the shared DB securities table")


def resolve_trade_close_price(
    ticker: str,
    trade_date: str | pd.Timestamp,
    *,
    shared_db: Path | str | None = None,
) -> float:
    symbol = resolve_security_symbol(ticker, shared_db=shared_db)
    if symbol == "CASH":
        return 1.0
    trade_text = pd.Timestamp(trade_date).normalize().strftime("%Y-%m-%d")
    target = _shared_db_path(shared_db)
    if not target.exists():
        raise ValueError(f"price is required because shared DB is missing: {target}")
    with sqlite3.connect(target) as conn:
        row = conn.execute(
            """
            SELECT close
            FROM prices
            WHERE symbol = ?
              AND date <= ?
              AND close IS NOT NULL
            ORDER BY date DESC
            LIMIT 1
            """,
            (symbol, trade_text),
        ).fetchone()
    if not row or row[0] is None:
        raise ValueError(f"close price not found for {symbol} on or before {trade_text}")
    price_value = float(row[0])
    if not np.isfinite(price_value) or price_value <= 0:
        raise ValueError(f"invalid close price for {symbol} on or before {trade_text}")
    return price_value


def ensure_portfolio_db(db_path: Path | str | None = None) -> Path:
    target = portfolio_db_path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(target) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
                quantity REAL NOT NULL CHECK(quantity > 0),
                price REAL NOT NULL CHECK(price > 0),
                fees REAL NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_portfolio_trades_date ON portfolio_trades(trade_date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_portfolio_trades_ticker_date ON portfolio_trades(ticker, trade_date)"
        )
        conn.commit()
    return target


def add_trade(
    *,
    trade_date: str,
    ticker: str,
    side: str,
    quantity: float,
    price: float | None,
    fees: float = 0.0,
    notes: str = "",
    db_path: Path | str | None = None,
    user_id: str | None = None,
    shared_db: Path | str | None = None,
) -> int:
    target = ensure_portfolio_db(db_path)
    side_clean = str(side or "").strip().upper()
    if side_clean not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    ticker_clean = resolve_security_symbol(ticker, shared_db=shared_db)
    trade_ts = pd.Timestamp(trade_date).normalize().strftime("%Y-%m-%d")
    quantity_value = float(quantity)
    price_value = float(price) if price is not None and str(price).strip() else 0.0
    if price_value <= 0:
        price_value = resolve_trade_close_price(ticker_clean, trade_ts, shared_db=shared_db)
    fee_value = float(fees)
    if quantity_value <= 0 or (not np.isfinite(price_value)) or price_value <= 0:
        raise ValueError("quantity and price must be positive")
    if fee_value < 0:
        raise ValueError("fees must be non-negative")
    with sqlite3.connect(target) as conn:
        cur = conn.execute(
            """
            INSERT INTO portfolio_trades(trade_date, ticker, side, quantity, price, fees, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (trade_ts, ticker_clean, side_clean, quantity_value, price_value, fee_value, str(notes or "").strip()),
        )
        conn.commit()
        return int(cur.lastrowid)


def delete_trade(
    trade_id: int,
    *,
    db_path: Path | str | None = None,
    user_id: str | None = None,
) -> bool:
    target = ensure_portfolio_db(db_path)
    trade_id_value = int(trade_id)
    if trade_id_value <= 0:
        raise ValueError("trade_id must be positive")
    with sqlite3.connect(target) as conn:
        cur = conn.execute(
            "DELETE FROM portfolio_trades WHERE id = ?",
            (trade_id_value,),
        )
        conn.commit()
        return int(cur.rowcount or 0) > 0


def load_trades(db_path: Path | str | None = None, *, user_id: str | None = None) -> pd.DataFrame:
    target = ensure_portfolio_db(db_path)
    with sqlite3.connect(target) as conn:
        frame = pd.read_sql_query(
            """
            SELECT id, trade_date, ticker, side, quantity, price, fees, notes, created_at
            FROM portfolio_trades
        ORDER BY trade_date ASC, CASE WHEN ticker = 'CASH' THEN 0 ELSE 1 END ASC, id ASC
            """,
            conn,
        )
    if frame.empty:
        return frame
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce")
    for col in ["quantity", "price", "fees"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["ticker"] = frame["ticker"].astype(str).str.upper()
    frame["side"] = frame["side"].astype(str).str.upper()
    frame["gross_amount"] = frame["quantity"] * frame["price"]
    # 현금(CASH) 입금은 플러스, 주식 매수는 마이너스로 표시되도록 수정
    frame["net_cash_flow"] = np.where(
        frame["ticker"].eq("CASH"),
        np.where(frame["side"].eq("BUY"), frame["gross_amount"], -frame["gross_amount"]),
        np.where(
            frame["side"].eq("BUY"),
            -(frame["gross_amount"] + frame["fees"]),
            frame["gross_amount"] - frame["fees"]
        )
    )
    return frame


def _load_component_frame(max_symbols: int = 0) -> tuple[pd.DataFrame, str]:
    requested = int(max_symbols)
    load_count = requested if requested > 0 else 10_000
    components, source = load_krx_components(max_symbols=load_count)
    out = components.copy()
    if out.empty and str(source).startswith("unavailable:"):
        target = _shared_db_path()
        if target.exists():
            try:
                query = """
                    SELECT symbol AS Symbol,
                           COALESCE(NULLIF(sector, ''), 'Unknown') AS Sector
                    FROM securities
                    WHERE COALESCE(is_active, 1) = 1
                    ORDER BY market ASC, symbol ASC
                """
                with sqlite3.connect(target) as conn:
                    out = pd.read_sql_query(query, conn)
                source = f"sqlite:{target.as_posix()}:securities"
            except Exception:
                out = components.copy()
    out["Symbol"] = out["Symbol"].astype(str).str.strip().str.upper()
    out["Sector"] = out["Sector"].astype(str).str.strip().replace({"": "Unknown", "nan": "Unknown"})
    out = out.dropna(subset=["Symbol"]).drop_duplicates(subset=["Symbol"], keep="last")
    if requested > 0:
        out = out.head(requested).reset_index(drop=True)
    return out, source


def _sector_map(max_symbols: int = 0) -> tuple[dict[str, str], str]:
    components, source = _load_component_frame(max_symbols=max_symbols)
    return dict(zip(components["Symbol"], components["Sector"])), source


def _latest_market_caps_for_symbols(
    symbols: list[str],
    *,
    shared_db: Path | str | None = None,
) -> dict[str, float]:
    normalized = [str(symbol or "").strip().upper() for symbol in symbols if str(symbol or "").strip()]
    if not normalized:
        return {}
    target = _shared_db_path(shared_db)
    if not target.exists():
        return {}
    placeholders = ",".join("?" for _ in normalized)
    query = f"""
        WITH latest_caps AS (
            SELECT symbol, MAX(date) AS date
            FROM prices
            WHERE symbol IN ({placeholders})
              AND market_cap IS NOT NULL
            GROUP BY symbol
        )
        SELECT p.symbol, p.market_cap
        FROM prices AS p
        INNER JOIN latest_caps AS latest
            ON latest.symbol = p.symbol
           AND latest.date = p.date
    """
    try:
        with sqlite3.connect(target) as conn:
            rows = conn.execute(query, normalized).fetchall()
    except Exception:
        return {}
    out: dict[str, float] = {}
    for symbol, market_cap in rows:
        try:
            value = float(market_cap)
        except Exception:
            continue
        if np.isfinite(value) and value > 0:
            out[str(symbol).strip().upper()] = value
    return out


def _load_kospi200_benchmark_frame(
    *,
    shared_db: Path | str | None = None,
    max_symbols: int = 200,
) -> tuple[pd.DataFrame, str]:
    target = _shared_db_path(shared_db)
    history = load_latest_index_constituent_history(DEFAULT_BENCHMARK_CODE, db_path=target)
    if history is not None and not history.empty:
        frame = history.rename(columns={"symbol": "Symbol", "sector": "Sector"}).copy()
        frame["Symbol"] = frame["Symbol"].astype(str).str.strip().str.upper()
        frame["Sector"] = frame["Sector"].astype(str).str.strip().replace({"": "Unknown", "nan": "Unknown"})
        caps = _latest_market_caps_for_symbols(frame["Symbol"].astype(str).tolist(), shared_db=shared_db)
        frame["market_cap"] = frame["Symbol"].map(caps).fillna(0.0)
        total = float(pd.to_numeric(frame["market_cap"], errors="coerce").fillna(0.0).sum())
        frame["benchmark_weight"] = (
            pd.to_numeric(frame["market_cap"], errors="coerce").fillna(0.0) / total
            if total > 0
            else 1.0 / len(frame.index)
        )
        return frame[["Symbol", "Sector", "benchmark_weight"]], f"sqlite:{target.as_posix()}:index_constituent_history:{DEFAULT_BENCHMARK_CODE}"

    snapshot = load_latest_krx_benchmark_snapshot(DEFAULT_BENCHMARK_CODE, db_path=target)
    if snapshot is not None and not snapshot.empty:
        frame = snapshot.rename(columns={"symbol": "Symbol", "sector": "Sector"}).copy()
        frame["Symbol"] = frame["Symbol"].astype(str).str.strip().str.upper()
        frame["Sector"] = frame["Sector"].astype(str).str.strip().replace({"": "Unknown", "nan": "Unknown"})
        frame["benchmark_weight"] = pd.to_numeric(frame.get("benchmark_weight"), errors="coerce")
        frame = frame.dropna(subset=["Symbol"]).drop_duplicates(subset=["Symbol"], keep="first")
        return frame[["Symbol", "Sector", "benchmark_weight"]], f"sqlite:{target.as_posix()}:benchmark_constituents:{DEFAULT_BENCHMARK_CODE}"

    if not target.exists():
        return pd.DataFrame(columns=["Symbol", "Sector", "benchmark_weight"]), "unavailable:kospi200_benchmark_missing"

    try:
        as_of_date = _get_db_max_date(shared_db).strftime("%Y-%m-%d")
        query = """
            WITH latest_caps AS (
                SELECT p.symbol, p.market_cap, p.date
                FROM prices AS p
                INNER JOIN (
                    SELECT symbol, MAX(date) AS date
                    FROM prices
                    WHERE date <= ?
                      AND market_cap IS NOT NULL
                    GROUP BY symbol
                ) AS latest
                    ON latest.symbol = p.symbol
                   AND latest.date = p.date
            )
            SELECT
                s.symbol AS Symbol,
                COALESCE(NULLIF(s.sector, ''), 'Unknown') AS Sector,
                latest_caps.market_cap AS market_cap
            FROM securities AS s
            INNER JOIN latest_caps
                ON latest_caps.symbol = s.symbol
            WHERE s.market = 'KOSPI'
              AND COALESCE(s.is_active, 1) = 1
              AND latest_caps.market_cap > 0
            ORDER BY latest_caps.market_cap DESC, s.symbol ASC
            LIMIT ?
        """
        with sqlite3.connect(target) as conn:
            frame = pd.read_sql_query(query, conn, params=[as_of_date, int(max_symbols)])
    except Exception:
        return pd.DataFrame(columns=["Symbol", "Sector", "benchmark_weight"]), "unavailable:kospi200_proxy_failed"

    if frame.empty:
        return pd.DataFrame(columns=["Symbol", "Sector", "benchmark_weight"]), "unavailable:kospi200_proxy_empty"
    frame["Symbol"] = frame["Symbol"].astype(str).str.strip().str.upper()
    frame["Sector"] = frame["Sector"].astype(str).str.strip().replace({"": "Unknown", "nan": "Unknown"})
    caps = pd.to_numeric(frame["market_cap"], errors="coerce").fillna(0.0)
    total = float(caps.sum())
    frame["benchmark_weight"] = caps / total if total > 0 else 1.0 / len(frame.index)
    return frame[["Symbol", "Sector", "benchmark_weight"]], f"sqlite:{target.as_posix()}:kospi_top200_proxy"


def _latest_news_signals(
    tickers: list[str],
    *,
    shared_db: Path | str | None = None,
    lookback_days: int = 30,
) -> pd.DataFrame:
    if not tickers:
        return pd.DataFrame(columns=["ticker", "recent_news_count", "avg_sentiment_score", "news_signal_score"])
    target = _shared_db_path(shared_db)
    if not target.exists():
        return pd.DataFrame(
            [{"ticker": ticker, "recent_news_count": 0, "avg_sentiment_score": np.nan, "news_signal_score": 50.0} for ticker in tickers]
        )
    placeholders = ",".join("?" for _ in tickers)
    db_now = _get_db_max_date(shared_db)
    start_date = (db_now - pd.Timedelta(days=max(int(lookback_days), 1))).strftime("%Y-%m-%d")
    with sqlite3.connect(target) as conn:
        try:
            news_columns = {
                str(row[1]).lower()
                for row in conn.execute("PRAGMA table_info(news_articles)").fetchall()
            }
        except Exception:
            news_columns = set()
    ticker_column = "ticker" if "ticker" in news_columns else "symbol" if "symbol" in news_columns else ""
    if not ticker_column:
        return pd.DataFrame(
            [{"ticker": ticker, "recent_news_count": 0, "avg_sentiment_score": np.nan, "news_signal_score": 50.0} for ticker in tickers]
        )
    query = f"""
        SELECT {ticker_column} AS ticker, publish_date, title, sentiment_score
        FROM news_articles
        WHERE {ticker_column} IN ({placeholders})
          AND date(publish_date) >= ?
        ORDER BY publish_date DESC, id DESC
    """
    params: list[object] = [*tickers, start_date]
    with sqlite3.connect(target) as conn:
        frame = pd.read_sql_query(query, conn, params=params)
    if frame.empty:
        return pd.DataFrame(
            [{"ticker": ticker, "recent_news_count": 0, "avg_sentiment_score": np.nan, "news_signal_score": 50.0} for ticker in tickers]
        )
    frame["ticker"] = frame["ticker"].astype(str).str.upper()
    frame["sentiment_score"] = pd.to_numeric(frame["sentiment_score"], errors="coerce")
    frame["heuristic_sentiment"] = frame["title"].map(heuristic_title_sentiment)
    frame["effective_sentiment"] = frame["sentiment_score"].fillna(frame["heuristic_sentiment"])
    summary = (
        frame.groupby("ticker", as_index=False)
        .agg(
            recent_news_count=("title", "size"),
            avg_sentiment_score=("effective_sentiment", "mean"),
        )
        .sort_values("ticker")
    )
    summary["news_signal_score"] = (
        50.0
        + summary["avg_sentiment_score"].fillna(0.0).clip(-4.0, 4.0) * 10.0
        + np.log1p(summary["recent_news_count"]).clip(0.0, 3.0) * 5.0
    ).clip(0.0, 100.0)
    return summary


def _load_optional_financial_metrics(
    tickers: list[str],
    *,
    shared_db: Path | str | None = None,
    as_of_date: str | pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, str]:
    db_target = _shared_db_path(shared_db)
    existing, source = derive_shared_fundamental_metrics(
        tickers,
        as_of_date=as_of_date,
        db_path=db_target,
    )
    if existing is not None and not existing.empty:
        frame = existing.rename(columns={"symbol": "ticker"}).copy()
        frame["ticker"] = frame["ticker"].astype(str).str.upper()
        keep_cols = [
            "ticker",
            "as_of_date",
            "price_date",
            "market_cap_date",
            "latest_fiscal_date",
            "latest_price",
            "market_cap",
            "ttm_net_income",
            "ttm_eps",
            "roe",
            "per",
            "pbr",
            "latest_equity",
            "average_equity",
            "latest_debt",
            "current_assets",
            "current_liabilities",
            "debt_to_equity",
            "current_ratio",
            "year_high",
            "year_low",
            "source",
        ]
        return frame[[col for col in keep_cols if col in frame.columns]], source or f"sqlite:{db_target.as_posix()}"
    empty = pd.DataFrame(columns=["ticker", "roe", "per", "pbr"])
    return empty, "not_available"


def _load_close_history(
    tickers: list[str],
    *,
    start_date: str,
    end_date: str | None = None,
    shared_db: Path | str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, str]]:
    if not tickers:
        return pd.DataFrame(), pd.DataFrame(), {"price_source": "empty", "market_cap_source": "empty"}
    close_df, close_source = load_shared_close_prices_for_symbols(
        tickers,
        start_date=start_date,
        end_date=end_date,
        db_path=_shared_db_path(shared_db),
    )
    caps_df, caps_source = load_shared_market_caps_for_symbols(
        tickers,
        start_date=start_date,
        end_date=end_date,
        db_path=_shared_db_path(shared_db),
    )
    close = close_df.copy() if close_df is not None else pd.DataFrame()
    caps = caps_df.copy() if caps_df is not None else pd.DataFrame()
    if not close.empty:
        close.index = pd.to_datetime(close.index, errors="coerce")
        close = close[~close.index.isna()].sort_index().apply(pd.to_numeric, errors="coerce").dropna(how="all", axis=1)
    if not caps.empty:
        caps.index = pd.to_datetime(caps.index, errors="coerce")
        caps = caps[~caps.index.isna()].sort_index().apply(pd.to_numeric, errors="coerce").dropna(how="all", axis=1)
    return close, caps, {
        "price_source": close_source or "sqlite",
        "market_cap_source": caps_source or "sqlite",
    }


def _current_positions_from_trades(
    trades: pd.DataFrame,
    *,
    sector_map: dict[str, str] | None = None,
) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(
            columns=[
                "ticker",
                "sector",
                "net_quantity",
                "avg_cost",
                "cost_basis",
                "realized_pnl",
                "trade_count",
                "last_trade_date",
            ]
        )
    state: dict[str, dict[str, any]] = {}
    cash_balance = 0.0
    # 같은 날짜 내에서는 현금(CASH) 처리를 최우선으로 하여 '아침에 입금된 효과'를 줍니다.
    temp_trades = trades.copy()
    temp_trades["_priority"] = np.where(temp_trades["ticker"] == "CASH", 0, 1)
    for row in temp_trades.sort_values(["trade_date", "_priority", "id"]).itertuples(index=False):
        ticker = str(row.ticker)
        side = str(row.side)
        qty = float(row.quantity)
        price = float(row.price)
        fees = float(row.fees)

        # 현금 티커 처리 (입금/출금)
        if ticker == "CASH":
            if side == "BUY": cash_balance += (qty * price)
            else: cash_balance -= (qty * price)
            continue

        item = state.setdefault(
            ticker,
            {
                "net_quantity": 0.0,
                "avg_cost": 0.0,
                "cost_basis": 0.0,
                "realized_pnl": 0.0,
                "trade_count": 0,
                "last_trade_date": None,
            },
        )
        current_qty = float(item["net_quantity"])
        current_avg = float(item["avg_cost"])
        if side == "BUY":
            total_cost = current_qty * current_avg + qty * price + fees
            next_qty = current_qty + qty
            next_avg = (total_cost / next_qty) if next_qty > 0 else 0.0
            item["net_quantity"] = next_qty
            item["avg_cost"] = next_avg
            item["cost_basis"] = next_qty * next_avg
            cash_balance -= (qty * price + fees)
        else:
            sell_qty = min(qty, current_qty) if current_qty > 0 else qty
            realized = (price - current_avg) * sell_qty - fees
            next_qty = current_qty - qty
            next_qty = max(next_qty, 0.0)
            item["realized_pnl"] = float(item["realized_pnl"]) + realized
            item["net_quantity"] = next_qty
            item["cost_basis"] = next_qty * current_avg
            item["avg_cost"] = current_avg if next_qty > 0 else 0.0
            cash_balance += (qty * price - fees)

        item["trade_count"] = int(item["trade_count"]) + 1
        item["last_trade_date"] = pd.Timestamp(row.trade_date)

    rows: list[dict[str, object]] = []
    # 현금 잔고를 포지션에 추가
    if cash_balance != 0:
        rows.append({
            "ticker": "CASH",
            "sector": "Cash",
            "net_quantity": cash_balance,
            "avg_cost": 1.0,
            "cost_basis": cash_balance,
            "realized_pnl": 0.0,
            "trade_count": 1,
            "last_trade_date": trades["trade_date"].max().strftime("%Y-%m-%d") if not trades.empty else None
        })

    sectors = sector_map or {}
    for ticker, item in state.items():
        qty = float(item["net_quantity"])
        if qty <= 0:
            continue
        rows.append(
            {
                "ticker": ticker,
                "sector": sectors.get(ticker, "Unknown"),
                "net_quantity": qty,
                "avg_cost": float(item["avg_cost"]),
                "cost_basis": float(item["cost_basis"]),
                "realized_pnl": float(item["realized_pnl"]),
                "trade_count": int(item["trade_count"]),
                "last_trade_date": pd.Timestamp(item["last_trade_date"]).strftime("%Y-%m-%d")
                if item["last_trade_date"] is not None
                else None,
            }
        )
    return pd.DataFrame(rows).sort_values("ticker").reset_index(drop=True) if rows else pd.DataFrame(columns=[
        "ticker",
        "sector",
        "net_quantity",
        "avg_cost",
        "cost_basis",
        "realized_pnl",
        "trade_count",
        "last_trade_date",
    ])


def _holding_returns(close_history: pd.DataFrame) -> pd.DataFrame:
    if close_history.empty:
        return pd.DataFrame()
    returns = close_history.sort_index().pct_change(fill_method=None)
    return returns.dropna(how="all")


def _period_return(series: pd.Series, window: int) -> float:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if len(clean) <= window:
        return np.nan
    tail = clean.tail(window + 1)
    if len(tail) < window + 1 or tail.iloc[0] == 0:
        return np.nan
    return float((tail.iloc[-1] / tail.iloc[0]) - 1.0)


def _max_drawdown(return_series: pd.Series) -> float:
    clean = pd.to_numeric(return_series, errors="coerce").dropna()
    if clean.empty:
        return np.nan
    wealth = (1.0 + clean).cumprod()
    peak = wealth.cummax()
    drawdown = wealth / peak - 1.0
    return float(drawdown.min())


def _weighted_return(series: pd.Series, weights: pd.Series) -> float:
    aligned_weights = weights.reindex(series.index).dropna()
    if aligned_weights.empty or aligned_weights.sum() == 0:
        return np.nan
    aligned_weights = aligned_weights / aligned_weights.sum()
    aligned_series = series.reindex(aligned_weights.index).fillna(0.0)
    return float((aligned_series * aligned_weights).sum())


def _normalize_weights(values: pd.Series) -> pd.Series:
    clean = pd.to_numeric(values, errors="coerce").fillna(0.0)
    total = float(clean.sum())
    if total <= 0:
        return pd.Series(0.0, index=clean.index)
    return clean / total


def _compound_return_pct_from_prices(series: pd.Series, window: int = 60) -> float:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if len(clean) < 2:
        return np.nan
    sample_window = min(max(int(window), 1), len(clean) - 1)
    if sample_window <= 0:
        return np.nan
    tail = clean.tail(sample_window + 1)
    if len(tail) < 2 or float(tail.iloc[0]) == 0.0:
        return np.nan
    return float((tail.iloc[-1] / tail.iloc[0] - 1.0) * 100.0)


def _compound_return_pct_from_daily(series: pd.Series, window: int = 60) -> float:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return np.nan
    tail = clean.tail(max(int(window), 1))
    if tail.empty:
        return np.nan
    return float(((1.0 + tail).prod() - 1.0) * 100.0)


def _fmt(value: object, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "-"
    try:
        numeric = float(value)
    except (ValueError, TypeError):
        return str(value)
    if not np.isfinite(numeric):
        return "-"
    return f"{numeric:,.{digits}f}{suffix}"


def _build_integrated_score_chart(scoring: pd.DataFrame) -> str:
    if scoring.empty: return ""
    # 상/하위 10개 합산 (중복 제거)
    top_10 = scoring.head(10)
    bottom_10 = scoring.tail(10)
    combined = pd.concat([bottom_10, top_10]).drop_duplicates(subset=["ticker"]).sort_values("integrated_score")
    
    fig, ax = plt.subplots(figsize=(10, 6))
    colors = ["#2e7d32" if s >= 50 else "#c62828" for s in combined["integrated_score"]]
    ax.barh(combined["ticker"], combined["integrated_score"], color=colors, alpha=0.85)
    ax.axvline(50, color='gray', linestyle='--', alpha=0.5)
    ax.set_title("Integrated Score: Top & Bottom Snapshot", fontsize=14, pad=10)
    ax.set_xlim(0, 100)
    ax.grid(axis="x", alpha=0.2)
    fig.tight_layout()
    return _render_chart_base64(fig)


def _build_scoring_commentary(scoring_held: pd.DataFrame, recommendations: pd.DataFrame) -> str:
    if scoring_held.empty and recommendations.empty:
        return "데이터가 부족하여 분석을 수행할 수 없습니다."
    
    parts = []
    if not scoring_held.empty:
        avg_score = scoring_held["integrated_score"].mean()
        parts.append(f"현재 보유 종목의 평균 통합 점수는 {_fmt(avg_score, 1)}점입니다.")
    
    if not recommendations.empty:
        top_ticker = recommendations.iloc[0]["ticker"]
        top_sector = recommendations.iloc[0]["sector"]
        parts.append(f"KRX 미보유 종목 중에서는 {top_sector} 섹터의 {top_ticker} 등이 기술적 추세와 재무 건전성 측면에서 가장 높은 점수를 기록하며 유력한 추천 후보로 나타났습니다.")
    
    parts.append("통합 점수는 가격 모멘텀(40%), 뉴스 신호(30%), 그리고 ROE/PER 등 재무 지표(30%)를 종합하여 산출됩니다.")
    parts.append("상위권 종목은 추세 우위와 펀더멘털이 결합된 매수 기회로, 하위권은 잠재적 위험 신호로 해석할 수 있습니다.")
    parts.append("시장 환경에 따라 점수가 변동될 수 있으므로, 주기적인 데이터 갱신을 통해 분석의 신선도를 유지하시기 바랍니다.")
    
    return " ".join(parts)


def _render_chart_base64(fig: plt.Figure) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=120)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _build_cumulative_return_chart(portfolio_daily: pd.Series, benchmark_daily: pd.Series) -> str:
    if portfolio_daily.empty or benchmark_daily.empty:
        return ""
    p_cum = (1.0 + portfolio_daily).cumprod() * 100.0
    b_cum = (1.0 + benchmark_daily).cumprod() * 100.0
    
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(p_cum.index, p_cum.values, label="Portfolio", color="#0f4c81", linewidth=2.5)
    ax.plot(b_cum.index, b_cum.values, label="KOSPI 200", color="#b26a00", linewidth=1.5, linestyle="--")
    
    ax.axhline(100, color="black", linewidth=0.8, alpha=0.5)
    ax.set_title("Cumulative Performance (Rebased to 100)", fontsize=12, pad=10)
    ax.legend(loc="upper left", frameon=False)
    ax.grid(True, alpha=0.2)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%y-%m-%d"))
    fig.tight_layout()
    return _render_chart_base64(fig)


def _build_sector_contribution_chart(attribution: pd.DataFrame) -> str:
    if attribution.empty or "sector" not in attribution.columns or "total_effect_pct" not in attribution.columns:
        return ""
    df = attribution[attribution["sector"] != "Total"].copy()
    if df.empty: return ""
    df = df.sort_values("total_effect_pct", ascending=True)
    
    fig, ax = plt.subplots(figsize=(6, 5))
    colors = ["#2e7d32" if x >= 0 else "#c62828" for x in df["total_effect_pct"]]
    ax.barh(df["sector"], df["total_effect_pct"], color=colors, alpha=0.8)
    ax.axvline(0, color="black", linewidth=0.8)
    ax.set_title("Sector Attribution Effect (%)", fontsize=11)
    ax.grid(axis="x", alpha=0.2)
    fig.tight_layout()
    return _render_chart_base64(fig)


def _build_style_exposure_chart(exposure: pd.DataFrame) -> str:
    if exposure.empty: return ""
    # 비중이 높은 순서대로 정렬
    df = exposure.sort_values("portfolio_weight_pct", ascending=False)
    
    fig, ax = plt.subplots(figsize=(6, 5))
    buckets = df["style_bucket"].astype(str).tolist()
    x = np.arange(len(buckets))
    width = 0.35
    
    # 'Cash' 항목이면 도넛 차트와 같은 초록색(#27ae60)을 사용하고, 나머지는 기본색(#0f4c81)을 유지합니다.
    p_colors = [SECTOR_COLOR_PALETTE["Cash"] if b == "Cash" else "#0f4c81" for b in buckets]

    ax.bar(x - width/2, df["portfolio_weight_pct"], width, color=p_colors)
    ax.bar(x + width/2, df["benchmark_weight_pct"], width, label='KOSPI 200', color="#b26a00", alpha=0.6)
    
    # 범례에 Cash 색상을 반영하기 위해 커스텀 핸들 생성
    legend_elements = []
    
    # 주식 항목이 있는 경우 파란색 핸들 추가
    if any(b != "Cash" for b in buckets):
        legend_elements.append(Patch(facecolor="#0f4c81", label='Portfolio (Stock)'))
    
    # 현금 항목이 있는 경우 초록색 핸들 추가
    if "Cash" in buckets:
        legend_elements.append(Patch(facecolor=SECTOR_COLOR_PALETTE["Cash"], label='Portfolio (Cash)'))
        
    # 벤치마크 핸들 추가
    legend_elements.append(Patch(facecolor="#b26a00", alpha=0.6, label='KOSPI 200'))

    ax.set_title("Style Exposure Comparison", fontsize=12)
    ax.set_ylabel("Weight (%)")
    ax.set_xticks(x)
    ax.set_xticklabels(buckets, rotation=15)
    ax.legend(handles=legend_elements, loc="upper right", frameon=False, fontsize=9)
    ax.grid(axis="y", alpha=0.2)
    fig.tight_layout()
    return _render_chart_base64(fig)


def _binary_treemap_layout(items: list[tuple[str, float]], x: float, y: float, width: float, height: float) -> list[tuple[str, float, float, float, float, float]]:
    filtered = [(str(label), float(value)) for label, value in items if float(value) > 0]
    if not filtered:
        return []
    if len(filtered) == 1:
        label, value = filtered[0]
        return [(label, value, x, y, width, height)]

    total = sum(value for _, value in filtered)
    if total <= 0:
        return []

    running = 0.0
    split_idx = 0
    half = total / 2.0
    for idx, (_, value) in enumerate(filtered, start=1):
        running += value
        split_idx = idx
        if running >= half:
            break

    left_items = filtered[:split_idx]
    right_items = filtered[split_idx:]
    if not right_items:
        left_items = filtered[:-1]
        right_items = filtered[-1:]

    left_total = sum(value for _, value in left_items)
    ratio = left_total / total if total > 0 else 1.0

    if width >= height:
        left_width = width * ratio
        return _binary_treemap_layout(left_items, x, y, left_width, height) + _binary_treemap_layout(
            right_items,
            x + left_width,
            y,
            max(width - left_width, 0.0),
            height,
        )

    top_height = height * ratio
    return _binary_treemap_layout(left_items, x, y, width, top_height) + _binary_treemap_layout(
        right_items,
        x,
        y + top_height,
        width,
        max(height - top_height, 0.0),
    )


def _monotone_treemap_colors(count: int, *, base_color: str) -> list[tuple[float, float, float]]:
    if count <= 0:
        return []
    base = np.array(mcolors.to_rgb(base_color), dtype=float)
    white = np.array([1.0, 1.0, 1.0], dtype=float)
    if count == 1:
        rgb = white * 0.32 + base * 0.68
        return [tuple(np.clip(rgb, 0.0, 1.0))]
    shades: list[tuple[float, float, float]] = []
    for idx in range(count):
        intensity = 1.0 - (idx / max(count - 1, 1))
        blend = 0.28 + (0.44 * intensity)
        rgb = white * (1.0 - blend) + base * blend
        shades.append(tuple(np.clip(rgb, 0.0, 1.0)))
    return shades


def _short_sector_label(label: str) -> str:
    mapping = {
        "Information Technology": "Info Tech",
        "Health Care": "Health",
        "Financials": "Financials",
        "Consumer Discretionary": "Cons Disc",
        "Communication Services": "Comm Svcs",
        "Industrials": "Industrials",
        "Consumer Staples": "Cons Staples",
        "Energy": "Energy",
        "Utilities": "Utilities",
        "Real Estate": "Real Estate",
        "Materials": "Materials",
        "Cash": "Cash",
        "Unknown": "Unknown",
    }
    text = str(label)
    return mapping.get(text, text)


def _treemap_label_text(label: str, weight_pct: float, width: float, height: float) -> tuple[str | None, int]:
    area = float(width) * float(height)
    short_label = _short_sector_label(label)
    min_side = min(float(width), float(height))
    max_side = max(float(width), float(height))

    if area < 0.024 or min_side < 0.06:
        return None, 0
    if area >= 0.075 and min_side >= 0.11 and max_side >= 0.18:
        return f"{short_label}\n{weight_pct:.1f}%", 11 if area >= 0.12 else 10
    if area >= 0.05 and min_side >= 0.09:
        return f"{short_label}\n{weight_pct:.1f}%", 10
    if area >= 0.034 and min_side >= 0.075:
        return short_label, 9
    return None, 0


def _build_sector_treemap_chart(alloc: pd.Series, *, title: str, base_color: str) -> str:
    clean = pd.to_numeric(alloc, errors="coerce").dropna()
    clean = clean[clean > 0].sort_values(ascending=False)
    if clean.empty:
        return ""

    fig, ax = plt.subplots(figsize=(8, 5.2))
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_title(title, fontsize=14, pad=12, loc="left")

    items = [(str(label), float(value)) for label, value in clean.items()]
    rects = _binary_treemap_layout(items, 0.0, 0.0, 1.0, 1.0)
    fills = _monotone_treemap_colors(len(rects), base_color=base_color)
    total = float(clean.sum())

    for idx, (label, value, x, y, width, height) in enumerate(rects):
        fill = fills[idx]
        rect = plt.Rectangle((x, y), width, height, facecolor=fill, edgecolor="#ffffff", linewidth=2.0)
        ax.add_patch(rect)

        weight_pct = (value / total) * 100.0 if total > 0 else 0.0
        label_text, fontsize = _treemap_label_text(label, weight_pct, width, height)
        if not label_text:
            continue

        ax.text(
            x + width / 2.0,
            y + height / 2.0,
            label_text,
            ha="center",
            va="center",
            color="#17324d",
            fontsize=fontsize,
            fontweight="bold",
            linespacing=1.15,
            wrap=True,
            clip_on=True,
        )

    fig.tight_layout(pad=0.6)
    return _render_chart_base64(fig)


def _build_sector_allocation_chart(positions: pd.DataFrame, sector_order: list[str] | None = None) -> str:
    if positions.empty or "market_value" not in positions.columns or "sector" not in positions.columns:
        return ""
    # 시장 가치가 있는 포지션만 집계 (현금 포함)
    df = positions[positions["market_value"] > 0].copy()
    if df.empty: return ""

    alloc = df.groupby("sector")["market_value"].sum()

    # 벤치마크(KRX)의 배치 순서에 강제로 맞춥니다.
    # 벤치마크에 없는 섹터(현금 등)는 목록 하단에 추가합니다.
    if sector_order:
        current_sectors = alloc.index.tolist()
        final_order = [s for s in sector_order if s in current_sectors]
        others = [s for s in current_sectors if s not in sector_order]
        alloc = alloc.reindex(final_order + others).fillna(0.0)
    else:
        alloc = alloc.sort_values(ascending=False)
    return _build_sector_treemap_chart(alloc, title="Asset Allocation by Sector", base_color="#0f4c81")


def _build_rebalancing_chart(current_positions: pd.DataFrame, target_alloc: pd.DataFrame, title: str) -> str:
    """현재 포트폴리오 비중과 최적화 목표 비중의 차이를 시각화합니다."""
    if target_alloc.empty or "ticker" not in target_alloc.columns or "target_weight_pct" not in target_alloc.columns:
        return ""
    
    # 현재 비중 (CASH 포함)
    curr = current_positions.set_index("ticker")["portfolio_weight"].astype(float) if not current_positions.empty else pd.Series(dtype=float)
    # 목표 비중
    tgt = target_alloc.set_index("ticker")["target_weight_pct"].astype(float)
    
    # 모든 티커 정렬 및 결합
    all_tickers = sorted(set(curr.index) | set(tgt.index))
    df = pd.DataFrame(index=all_tickers)
    df["current"] = curr.reindex(all_tickers).fillna(0.0)
    df["target"] = tgt.reindex(all_tickers).fillna(0.0)
    df["delta"] = df["target"] - df["current"]
    
    # 유의미한 변화(0.1% 이상)만 필터링하여 상위/하위 10개씩 추출
    df = df[df["delta"].abs() > 0.1].sort_values("delta", ascending=True)
    if df.empty: return ""
    if len(df) > 20:
        df = pd.concat([df.head(10), df.tail(10)]).drop_duplicates().sort_values("delta")

    fig, ax = plt.subplots(figsize=(8, 6))
    colors = ["#c62828" if x < 0 else "#2e7d32" for x in df["delta"]]
    ax.barh(df.index, df["delta"], color=colors, alpha=0.8)
    ax.axvline(0, color="black", linewidth=0.8)
    ax.set_title(title, fontsize=12, pad=10)
    ax.set_xlabel("Weight Change (%)")
    ax.grid(axis="x", alpha=0.2, linestyle='--')
    fig.tight_layout()
    return _render_chart_base64(fig)


def _build_benchmark_sector_allocation_chart(benchmark_weights: pd.Series, sector_frame: pd.DataFrame) -> tuple[str, list[str]]:
    if benchmark_weights.empty or sector_frame.empty:
        return "", []

    sector_lookup = dict(zip(sector_frame["Symbol"], sector_frame["Sector"]))
    w_df = benchmark_weights.to_frame("weight")
    w_df["sector"] = w_df.index.map(sector_lookup).fillna("Unknown")

    alloc = w_df.groupby("sector")["weight"].sum().sort_values(ascending=False)

    chart = _build_sector_treemap_chart(alloc, title="KOSPI 200 Sector Weights", base_color="#b26a00")
    return chart, alloc.index.tolist()


def _build_risk_contribution_chart(contributions: pd.DataFrame) -> str:
    if contributions.empty or "ticker" not in contributions.columns or "risk_contribution_pct" not in contributions.columns:
        return ""
    # 기여도가 높은 상위 15개 종목만 표시
    df = contributions.head(15).copy().sort_values("risk_contribution_pct", ascending=True)
    
    fig, ax = plt.subplots(figsize=(7, 5))
    # 종목(Ticker)별로 고유하고 일관된 색상을 할당하여 시각적 직관성 강화
    cmap = plt.get_cmap('tab20')
    colors = [cmap(int(hashlib.md5(t.encode()).hexdigest(), 16) % 20) for t in df["ticker"]]
    
    bars = ax.barh(df["ticker"], df["risk_contribution_pct"], color=colors, alpha=0.85, edgecolor='white', linewidth=0.5)
    
    ax.set_title("Absolute Risk Contribution (%)", fontsize=12, pad=10)
    ax.set_xlabel("Contribution to Total Portfolio Variance (%)")
    ax.grid(axis="x", alpha=0.2, linestyle='--')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    fig.tight_layout()
    return _render_chart_base64(fig)


def _build_active_risk_contribution_chart(contributions: pd.DataFrame) -> str:
    if contributions.empty or "ticker" not in contributions.columns or "active_risk_contribution_pct" not in contributions.columns:
        return ""
    # 기여도가 높은 상위 15개 종목만 표시
    df = contributions.head(15).copy().sort_values("active_risk_contribution_pct", ascending=True)
    
    fig, ax = plt.subplots(figsize=(7, 5))
    # 종목(Ticker)별로 고유하고 일관된 색상을 할당 (절대 리스크 차트와 색상 동기화)
    cmap = plt.get_cmap('tab20')
    colors = [cmap(int(hashlib.md5(t.encode()).hexdigest(), 16) % 20) for t in df["ticker"]]

    bars = ax.barh(df["ticker"], df["active_risk_contribution_pct"], color=colors, alpha=0.85, edgecolor='white', linewidth=0.5)
    
    ax.set_title("Active Risk Contribution (%)", fontsize=12, pad=10)
    ax.set_xlabel("Contribution to Tracking Error Variance (%)")
    ax.grid(axis="x", alpha=0.2, linestyle='--')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    fig.tight_layout()
    return _render_chart_base64(fig)


def _classify_style_bucket(per: float | None, pbr: float | None, roe: float | None) -> str:
    per_value = float(per) if per is not None and pd.notna(per) else np.nan
    pbr_value = float(pbr) if pbr is not None and pd.notna(pbr) else np.nan
    roe_value = float(roe) if roe is not None and pd.notna(roe) else np.nan
    if not np.isfinite(per_value) and not np.isfinite(pbr_value) and not np.isfinite(roe_value):
        return "Unknown"
    value_votes = int(np.isfinite(per_value) and per_value <= 18.0) + int(np.isfinite(pbr_value) and pbr_value <= 3.0)
    growth_votes = int(np.isfinite(per_value) and per_value >= 24.0) + int(np.isfinite(pbr_value) and pbr_value >= 4.0)
    quality_votes = int(np.isfinite(roe_value) and roe_value >= 0.20) + int(np.isfinite(roe_value) and roe_value >= 0.30)
    if value_votes >= 2 and quality_votes == 0:
        return "Value"
    if growth_votes >= 1 and quality_votes >= 1:
        return "Growth"
    if quality_votes >= 1 and value_votes == 0:
        return "Quality"
    if value_votes >= 1 and quality_votes >= 1:
        return "Value-Quality"
    return "Blend"


def _build_style_map(
    symbols: list[str],
    *,
    shared_db: Path | str | None = None,
    as_of_date: str | pd.Timestamp | None = None,
) -> tuple[dict[str, str], pd.DataFrame, str]:
    financial, source = _load_optional_financial_metrics(symbols, shared_db=shared_db, as_of_date=as_of_date)
    if financial.empty:
        return {}, pd.DataFrame(columns=["ticker", "style_bucket", "roe", "per", "pbr"]), source
    frame = financial.rename(columns={"ticker": "ticker"}).copy()
    frame["ticker"] = frame["ticker"].astype(str).str.upper()
    frame["style_bucket"] = [
        _classify_style_bucket(per, pbr, roe)
        for per, pbr, roe in zip(frame["per"], frame["pbr"], frame["roe"])
    ]
    return dict(zip(frame["ticker"], frame["style_bucket"])), frame[["ticker", "style_bucket", "roe", "per", "pbr"]], source


def _build_positions_frame(
    positions: pd.DataFrame,
    *,
    close_history: pd.DataFrame,
) -> tuple[pd.DataFrame, str | None]:
    if positions.empty:
        return positions.copy(), None
    if close_history.empty:
        enriched = positions.copy()
        for col in ["last_close", "market_value", "unrealized_pnl", "total_pnl", "return_pct", "portfolio_weight"]:
            if col not in enriched.columns:
                enriched[col] = np.nan
        return enriched, None
    latest_date = pd.Timestamp(close_history.index.max()).strftime("%Y-%m-%d")
    latest_close = close_history.ffill().iloc[-1]
    enriched = positions.copy()
    enriched["last_close"] = enriched["ticker"].map(latest_close.to_dict())
    # 현금 가격은 항상 1.0으로 고정
    enriched.loc[enriched["ticker"] == "CASH", "last_close"] = 1.0

    enriched["market_value"] = enriched["net_quantity"] * enriched["last_close"]
    enriched["unrealized_pnl"] = enriched["market_value"] - enriched["cost_basis"]
    enriched["total_pnl"] = enriched["unrealized_pnl"] + enriched["realized_pnl"]
    enriched["return_pct"] = np.where(
        enriched["cost_basis"] > 0,
        enriched["unrealized_pnl"] / enriched["cost_basis"] * 100.0,
        np.nan,
    )
    enriched["portfolio_weight"] = _normalize_weights(enriched["market_value"]) * 100.0
    return enriched.sort_values("market_value", ascending=False).reset_index(drop=True), latest_date


def _build_holdings_performance(
    positions: pd.DataFrame,
    close_history: pd.DataFrame,
) -> pd.DataFrame:
    if positions.empty or close_history.empty:
        return pd.DataFrame()
    ref_ts = pd.Timestamp(close_history.index.max()).normalize()
    wtd_start = ref_ts - pd.Timedelta(days=ref_ts.dayofweek)
    mtd_start = ref_ts.replace(day=1)
    ytd_start = ref_ts.replace(month=1, day=1)

    def _series_period_return_pct(series: pd.Series, start_date: pd.Timestamp) -> float:
        clean = pd.to_numeric(series, errors="coerce").dropna().sort_index()
        if clean.empty:
            return np.nan
        subset = clean[clean.index >= start_date.normalize()]
        if len(subset) < 2 or float(subset.iloc[0]) == 0.0:
            return np.nan
        return float((subset.iloc[-1] / subset.iloc[0] - 1.0) * 100.0)

    rows: list[dict[str, object]] = []
    for row in positions.itertuples(index=False):
        ticker = str(row.ticker)
        if ticker == "CASH":
            rows.append({
                "ticker": "CASH",
                "sector": "Cash",
                "portfolio_weight_pct": float(row.portfolio_weight),
                "last_close": 1.0,
                "avg_cost": 1.0,
                "market_value": float(row.market_value),
                "unrealized_pnl": 0.0,
                "return_pct": 0.0,
                "selected_return_pct": 0.0,
                "return_wtd_pct": 0.0,
                "return_mtd_pct": 0.0,
                "return_20d_pct": 0.0,
                "return_60d_pct": 0.0,
                "return_ytd_pct": 0.0,
            })
            continue
        series = close_history[ticker] if ticker in close_history.columns else pd.Series(dtype=float)
        if series.empty:
            continue
        rows.append(
            {
                "ticker": ticker,
                "sector": row.sector,
                "portfolio_weight_pct": float(row.portfolio_weight),
                "last_close": float(row.last_close),
                "avg_cost": float(row.avg_cost),
                "market_value": float(row.market_value),
                "unrealized_pnl": float(row.unrealized_pnl),
                "return_pct": float(row.return_pct),
                "selected_return_pct": _series_period_return_pct(series, pd.Timestamp(series.index.min()).normalize()),
                "return_wtd_pct": _series_period_return_pct(series, wtd_start),
                "return_mtd_pct": _series_period_return_pct(series, mtd_start),
                "return_20d_pct": _period_return(series, 20) * 100.0,
                "return_60d_pct": _period_return(series, 60) * 100.0,
                "return_ytd_pct": _series_period_return_pct(series, ytd_start),
            }
        )
    return pd.DataFrame(rows).sort_values("market_value", ascending=False).reset_index(drop=True)


def _portfolio_return_series(
    close_history: pd.DataFrame,
    positions: pd.DataFrame,
) -> pd.Series:
    if close_history.empty or positions.empty:
        return pd.Series(dtype=float)
    weights = _normalize_weights(positions.set_index("ticker")["market_value"])
    # 현금(CASH)을 제외한 주식 종목만 수익률 계산에 참여 (현금 수익률은 0이므로 가중치만 반영됨)
    common = [ticker for ticker in weights.index if ticker in close_history.columns and ticker != "CASH"]
    if not common:
        return pd.Series(dtype=float)

    returns = close_history[common].sort_index().pct_change(fill_method=None)
    weights = weights.reindex(common).fillna(0.0)
    portfolio_daily = returns.mul(weights, axis=1).sum(axis=1, min_count=1)
    return portfolio_daily.dropna()


def _benchmark_series(
    *,
    close_history: pd.DataFrame,
    market_caps: pd.DataFrame,
    sector_frame: pd.DataFrame,
    benchmark_weights: pd.Series | None = None,
) -> tuple[pd.Series, pd.DataFrame, pd.Series]:
    if close_history.empty:
        return pd.Series(dtype=float), pd.DataFrame(), pd.Series(dtype=float)
    universe_returns = close_history.sort_index().pct_change(fill_method=None)
    if universe_returns.empty:
        return pd.Series(dtype=float), pd.DataFrame(), pd.Series(dtype=float)
    if benchmark_weights is not None and not benchmark_weights.empty:
        weights = _normalize_weights(pd.to_numeric(benchmark_weights, errors="coerce").reindex(universe_returns.columns))
    elif not market_caps.empty:
        latest_caps = market_caps.ffill().iloc[-1].reindex(universe_returns.columns)
        weights = _normalize_weights(latest_caps)
    else:
        weights = pd.Series(1.0 / len(universe_returns.columns), index=universe_returns.columns)
    if float(weights.sum()) <= 0:
        weights = pd.Series(1.0 / len(universe_returns.columns), index=universe_returns.columns)
    benchmark = universe_returns.mul(weights, axis=1).sum(axis=1, min_count=1).dropna()
    sector_lookup = dict(zip(sector_frame["Symbol"], sector_frame["Sector"]))
    sector_rows: list[pd.Series] = []
    for sector in sorted(set(sector_lookup.values())):
        members = [symbol for symbol, value in sector_lookup.items() if value == sector and symbol in universe_returns.columns]
        if not members:
            continue
        sector_weights = weights.reindex(members).fillna(0.0)
        if sector_weights.sum() <= 0:
            sector_weights = pd.Series(1.0 / len(members), index=members)
        else:
            sector_weights = sector_weights / sector_weights.sum()
        sector_series = universe_returns[members].mul(sector_weights, axis=1).sum(axis=1, min_count=1)
        sector_series.name = sector
        sector_rows.append(sector_series)
    sector_returns = pd.concat(sector_rows, axis=1).dropna(how="all") if sector_rows else pd.DataFrame()
    return benchmark, sector_returns, weights


def _build_portfolio_summary(
    positions: pd.DataFrame,
    holdings_performance: pd.DataFrame,
    portfolio_daily: pd.Series,
    benchmark_daily: pd.Series,
    *,
    as_of_date: str | None,
    start_ts: pd.Timestamp | None = None,
    end_ts: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if positions.empty:
        return pd.DataFrame(
            [
                {
                    "as_of_date": as_of_date,
                    "selected_start_date": start_ts.strftime("%Y-%m-%d") if start_ts else None,
                    "selected_end_date": end_ts.strftime("%Y-%m-%d") if end_ts else None,
                    "holding_count": 0,
                    "market_value": 0.0,
                    "cost_basis": 0.0,
                    "unrealized_pnl": 0.0,
                    "total_return_pct": np.nan,
                    "portfolio_return_20d_pct": np.nan,
                    "portfolio_return_60d_pct": np.nan,
                    "portfolio_vol_annual_pct": np.nan,
                    "benchmark_beta": np.nan,
                }
            ]
        )
    market_value = float(positions["market_value"].sum())
    cost_basis = float(positions["cost_basis"].sum())
    unrealized_pnl = float(positions["unrealized_pnl"].sum())
    total_return_pct = (unrealized_pnl / cost_basis * 100.0) if cost_basis > 0 else np.nan
    vol = float(portfolio_daily.std(ddof=1) * np.sqrt(252) * 100.0) if len(portfolio_daily) > 1 else np.nan
    beta = np.nan
    joined = pd.concat([portfolio_daily.rename("p"), benchmark_daily.rename("b")], axis=1).dropna()
    if len(joined) > 2 and float(joined["b"].var()) > 0:
        beta = float(joined["p"].cov(joined["b"]) / joined["b"].var())

    # 캘린더 기반 수익률 (WTD, MTD, YTD) 계산을 위한 기준일 설정
    ref_ts = end_ts if end_ts is not None else (pd.to_datetime(as_of_date) if as_of_date else pd.Timestamp.today().normalize())
    
    def _calc_period_ret_pct(daily: pd.Series, start_date: pd.Timestamp) -> float:
        if daily.empty: return np.nan
        # 기준일 이후의 데이터만 추출하여 누적 수익률 계산
        subset = daily[daily.index >= start_date.normalize()]
        if subset.empty: return np.nan
        return float(((1.0 + subset).prod() - 1.0) * 100.0)

    wtd_start = ref_ts - pd.Timedelta(days=ref_ts.dayofweek) # 해당 주의 월요일
    mtd_start = ref_ts.replace(day=1)                      # 해당 월의 1일
    ytd_start = ref_ts.replace(month=1, day=1)             # 해당 연도의 1월 1일

    row = {
        "as_of_date": as_of_date,
        "selected_start_date": start_ts.strftime("%Y-%m-%d") if start_ts else None,
        "selected_end_date": end_ts.strftime("%Y-%m-%d") if end_ts else None,
        "holding_count": int(len(positions.index)),
        "market_value": market_value,
        "cost_basis": cost_basis,
        "unrealized_pnl": unrealized_pnl,
        "realized_pnl": float(positions["realized_pnl"].sum()),
        "total_return_pct": total_return_pct,
        "portfolio_return_selected_pct": float(((1.0 + portfolio_daily).prod() - 1.0) * 100.0) if not portfolio_daily.empty else np.nan,
        "portfolio_return_wtd_pct": _calc_period_ret_pct(portfolio_daily, wtd_start),
        "portfolio_return_mtd_pct": _calc_period_ret_pct(portfolio_daily, mtd_start),
        "portfolio_return_ytd_pct": _calc_period_ret_pct(portfolio_daily, ytd_start),
        "portfolio_return_20d_pct": _period_return((1.0 + portfolio_daily).cumprod(), 20) * 100.0 if not portfolio_daily.empty else np.nan,
        "portfolio_return_60d_pct": _period_return((1.0 + portfolio_daily).cumprod(), 60) * 100.0 if not portfolio_daily.empty else np.nan,
        "portfolio_vol_annual_pct": vol,
        "benchmark_beta": beta,
        "max_drawdown_pct": _max_drawdown(portfolio_daily) * 100.0 if not portfolio_daily.empty else np.nan,
    }
    return pd.DataFrame([row])


def _build_attribution(
    positions: pd.DataFrame,
    holdings_performance: pd.DataFrame,
    benchmark_returns: pd.Series,
    benchmark_weights: pd.Series,
    sector_frame: pd.DataFrame,
) -> pd.DataFrame:
    if positions.empty or holdings_performance.empty or benchmark_weights.empty:
        return pd.DataFrame()
    portfolio_sector = (
        holdings_performance.groupby("sector", as_index=False)
        .agg(
            portfolio_weight_pct=("portfolio_weight_pct", "sum"),
            portfolio_return_pct=("return_60d_pct", "mean"),
            holding_count=("ticker", "size"),
        )
    )
    benchmark_total_return_pct = _compound_return_pct_from_daily(benchmark_returns, 60)
    sector_lookup = dict(zip(sector_frame["Symbol"], sector_frame["Sector"]))
    benchmark_sector_rows: list[dict[str, object]] = []
    for sector in sorted(set(sector_lookup.values())):
        members = [symbol for symbol, value in sector_lookup.items() if value == sector and symbol in benchmark_weights.index]
        if not members:
            continue
        sector_weight_pct = float(benchmark_weights.reindex(members).fillna(0.0).sum() * 100.0)
        benchmark_sector_rows.append(
            {
                "sector": sector,
                "benchmark_weight_pct": sector_weight_pct,
                "benchmark_return_pct": benchmark_total_return_pct,
            }
        )
    benchmark_sector = pd.DataFrame(benchmark_sector_rows)
    if benchmark_sector.empty:
        return portfolio_sector
    merged = portfolio_sector.merge(benchmark_sector, on="sector", how="outer").fillna(
        {"portfolio_weight_pct": 0.0, "portfolio_return_pct": 0.0, "holding_count": 0, "benchmark_weight_pct": 0.0}
    )
    merged["allocation_effect_pct"] = (
        (merged["portfolio_weight_pct"] - merged["benchmark_weight_pct"]) * merged["benchmark_return_pct"] / 100.0
    )
    merged["selection_effect_pct"] = (
        merged["portfolio_weight_pct"] * (merged["portfolio_return_pct"] - merged["benchmark_return_pct"]) / 100.0
    )
    merged["total_effect_pct"] = merged["allocation_effect_pct"] + merged["selection_effect_pct"]
    totals = {
        "sector": "Total",
        "portfolio_weight_pct": float(merged["portfolio_weight_pct"].sum()),
        "portfolio_return_pct": np.nan,
        "holding_count": int(merged["holding_count"].sum()),
        "benchmark_weight_pct": float(merged["benchmark_weight_pct"].sum()),
        "benchmark_return_pct": float(benchmark_total_return_pct) if pd.notna(benchmark_total_return_pct) else np.nan,
        "allocation_effect_pct": float(merged["allocation_effect_pct"].sum()),
        "selection_effect_pct": float(merged["selection_effect_pct"].sum()),
        "total_effect_pct": float(merged["total_effect_pct"].sum()),
    }
    return pd.concat([merged, pd.DataFrame([totals])], ignore_index=True)


def _build_relative_attribution_tables(
    positions: pd.DataFrame,
    close_history: pd.DataFrame,
    benchmark_close: pd.DataFrame,
    benchmark_weights: pd.Series,
    sector_frame: pd.DataFrame,
    *,
    style_map: dict[str, str],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if positions.empty or benchmark_close.empty or benchmark_weights.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    position_weights = _normalize_weights(positions.set_index("ticker")["market_value"])
    sector_lookup = dict(zip(sector_frame["Symbol"], sector_frame["Sector"]))
    combined_symbols = sorted(set(benchmark_close.columns).union(set(position_weights.index)))
    stock_rows: list[dict[str, object]] = []
    for symbol in combined_symbols:
        if symbol == "CASH":
            total_return_pct = 0.0
        else:
            price_series = benchmark_close[symbol] if symbol in benchmark_close.columns else (
                close_history[symbol] if symbol in close_history.columns else pd.Series(dtype=float)
            )
            total_return_pct = _compound_return_pct_from_prices(price_series, 60)
            
        portfolio_weight = float(position_weights.get(symbol, 0.0))
        benchmark_weight = float(benchmark_weights.get(symbol, 0.0))
        active_weight = portfolio_weight - benchmark_weight
        is_cash = symbol == "CASH"
        stock_rows.append(
            {
                "ticker": symbol,
                "sector": "Cash" if is_cash else sector_lookup.get(symbol, "Unknown"),
                "style_bucket": "Cash" if is_cash else style_map.get(symbol, "Unknown"),
                "portfolio_weight_pct": portfolio_weight * 100.0,
                "benchmark_weight_pct": benchmark_weight * 100.0,
                "active_weight_pct": active_weight * 100.0,
                "return_60d_pct": total_return_pct,
                "portfolio_contribution_pct": portfolio_weight * np.nan_to_num(total_return_pct, nan=0.0),
                "benchmark_contribution_pct": benchmark_weight * np.nan_to_num(total_return_pct, nan=0.0),
                "active_contribution_pct": active_weight * np.nan_to_num(total_return_pct, nan=0.0),
            }
        )
    stock_attribution = pd.DataFrame(stock_rows).sort_values(
        ["active_contribution_pct", "active_weight_pct"], ascending=[False, False]
    ).reset_index(drop=True)

    def _group_relative(frame: pd.DataFrame, group_col: str, benchmark_total_return_pct: float) -> pd.DataFrame:
        rows: list[dict[str, object]] = []
        for group_value, sub in frame.groupby(group_col, dropna=False):
            portfolio_weight = float(sub["portfolio_weight_pct"].sum() / 100.0)
            benchmark_weight = float(sub["benchmark_weight_pct"].sum() / 100.0)
            if portfolio_weight > 0:
                portfolio_return = float((sub["return_60d_pct"].fillna(0.0) * (sub["portfolio_weight_pct"] / 100.0)).sum() / portfolio_weight)
            else:
                portfolio_return = np.nan
            if benchmark_weight > 0:
                benchmark_return = float((sub["return_60d_pct"].fillna(0.0) * (sub["benchmark_weight_pct"] / 100.0)).sum() / benchmark_weight)
            else:
                benchmark_return = np.nan
            allocation = (portfolio_weight - benchmark_weight) * (
                np.nan_to_num(benchmark_return, nan=0.0) - np.nan_to_num(benchmark_total_return_pct, nan=0.0)
            )
            selection = benchmark_weight * (np.nan_to_num(portfolio_return, nan=0.0) - np.nan_to_num(benchmark_return, nan=0.0))
            interaction = (portfolio_weight - benchmark_weight) * (
                np.nan_to_num(portfolio_return, nan=0.0) - np.nan_to_num(benchmark_return, nan=0.0)
            )
            rows.append(
                {
                    group_col: group_value,
                    "portfolio_weight_pct": portfolio_weight * 100.0,
                    "benchmark_weight_pct": benchmark_weight * 100.0,
                    "active_weight_pct": (portfolio_weight - benchmark_weight) * 100.0,
                    "portfolio_return_pct": portfolio_return,
                    "benchmark_return_pct": benchmark_return,
                    "allocation_effect_pct": allocation,
                    "selection_effect_pct": selection,
                    "interaction_effect_pct": interaction,
                    "total_effect_pct": allocation + selection + interaction,
                }
            )
        grouped = pd.DataFrame(rows)
        if grouped.empty:
            return grouped
        total_row = {
            group_col: "Total",
            "portfolio_weight_pct": float(grouped["portfolio_weight_pct"].sum()),
            "benchmark_weight_pct": float(grouped["benchmark_weight_pct"].sum()),
            "active_weight_pct": float(grouped["active_weight_pct"].sum()),
            "portfolio_return_pct": np.nan,
            "benchmark_return_pct": benchmark_total_return_pct,
            "allocation_effect_pct": float(grouped["allocation_effect_pct"].sum()),
            "selection_effect_pct": float(grouped["selection_effect_pct"].sum()),
            "interaction_effect_pct": float(grouped["interaction_effect_pct"].sum()),
            "total_effect_pct": float(grouped["total_effect_pct"].sum()),
        }
        return pd.concat([grouped.sort_values("total_effect_pct", ascending=False), pd.DataFrame([total_row])], ignore_index=True)

    benchmark_total_return_pct = _compound_return_pct_from_prices(
        (1.0 + benchmark_close.pct_change(fill_method=None).mul(benchmark_weights.reindex(benchmark_close.columns).fillna(0.0), axis=1).sum(axis=1, min_count=1).dropna()).cumprod(),
        60,
    )
    sector_attribution = _group_relative(stock_attribution, "sector", benchmark_total_return_pct)
    style_attribution = _group_relative(stock_attribution, "style_bucket", benchmark_total_return_pct)
    return stock_attribution, sector_attribution, style_attribution


def _build_risk_summary(
    positions: pd.DataFrame,
    holding_returns: pd.DataFrame,
    portfolio_daily: pd.Series,
    benchmark_daily: pd.Series,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if positions.empty or holding_returns.empty or portfolio_daily.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    weights = _normalize_weights(positions.set_index("ticker")["market_value"])

    ticker_to_sector = positions.set_index("ticker")["sector"].to_dict()

    # 리스크 계산 시 현금(CASH)을 0 수익률 자산으로 명시적으로 포함하여 총 리스크 감소 효과 확인
    math_returns = holding_returns.copy()
    if "CASH" in weights.index and "CASH" not in math_returns.columns:
        math_returns["CASH"] = 0.0

    columns = [ticker for ticker in weights.index if ticker in math_returns.columns]
    if not columns:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    aligned = math_returns[columns].dropna(how="all")
    weights = weights.reindex(columns).fillna(0.0)
    cov = aligned.cov() * 252.0
    w = weights.to_numpy(dtype=float)
    cov_values = cov.to_numpy(dtype=float)
    portfolio_var = float(w.T @ cov_values @ w)
    portfolio_vol = float(np.sqrt(portfolio_var)) if portfolio_var > 0 else np.nan
    contributions = pd.DataFrame()
    if np.isfinite(portfolio_vol) and portfolio_vol > 0:
        marginal = cov_values @ w / portfolio_vol
        component = w * marginal
        contributions = pd.DataFrame(
            {
                "ticker": columns,
                "sector": [ticker_to_sector.get(t, "Unknown") for t in columns],
                "portfolio_weight_pct": weights.reindex(columns).to_numpy(dtype=float) * 100.0,
                "component_vol_pct": component * 100.0,
                "risk_contribution_pct": np.where(portfolio_vol > 0, component / portfolio_vol * 100.0, np.nan),
            }
        ).sort_values("risk_contribution_pct", ascending=False)
    joined = pd.concat([portfolio_daily.rename("p"), benchmark_daily.rename("b")], axis=1).dropna()
    beta = np.nan
    if len(joined) > 2 and float(joined["b"].var()) > 0:
        beta = float(joined["p"].cov(joined["b"]) / joined["b"].var())
    tracking_error = float((joined["p"] - joined["b"]).std(ddof=1) * np.sqrt(252) * 100.0) if len(joined) > 2 else np.nan
    active_return_pct = _compound_return_pct_from_daily(joined["p"] - joined["b"], min(len(joined), 60)) if len(joined) > 1 else np.nan
    information_ratio = (active_return_pct / tracking_error) if np.isfinite(tracking_error) and tracking_error not in (0.0, np.nan) else np.nan
    var_95 = float(portfolio_daily.quantile(0.05) * 100.0) if len(portfolio_daily) > 5 else np.nan
    cvar_95 = float(portfolio_daily[portfolio_daily <= portfolio_daily.quantile(0.05)].mean() * 100.0) if len(portfolio_daily) > 5 else np.nan
    summary = pd.DataFrame(
        [
            {
                "annualized_vol_pct": portfolio_vol * 100.0 if np.isfinite(portfolio_vol) else np.nan,
                "beta_to_krx": beta,
                "daily_var_95_pct": var_95,
                "daily_cvar_95_pct": cvar_95,
                "max_drawdown_pct": _max_drawdown(portfolio_daily) * 100.0,
                "best_day_pct": float(portfolio_daily.max() * 100.0),
                "worst_day_pct": float(portfolio_daily.min() * 100.0),
            }
        ]
    )
    relative_summary = pd.DataFrame(
        [
            {
                "benchmark_return_60d_pct": _compound_return_pct_from_daily(benchmark_daily, 60),
                "portfolio_return_60d_pct": _compound_return_pct_from_daily(portfolio_daily, 60),
                "active_return_60d_pct": active_return_pct,
                "tracking_error_annual_pct": tracking_error,
                "information_ratio": information_ratio,
                "benchmark_vol_annual_pct": float(benchmark_daily.std(ddof=1) * np.sqrt(252) * 100.0) if len(benchmark_daily) > 1 else np.nan,
                "correlation_to_krx": float(joined["p"].corr(joined["b"])) if len(joined) > 2 else np.nan,
            }
        ]
    )
    return summary, contributions.reset_index(drop=True), relative_summary


def _build_active_risk_contribution(
    positions: pd.DataFrame,
    benchmark_weights: pd.Series,
    benchmark_close: pd.DataFrame,
    sector_frame: pd.DataFrame,
    *,
    style_map: dict[str, str],
) -> pd.DataFrame:
    if benchmark_close.empty or benchmark_weights.empty:
        return pd.DataFrame()
    returns = benchmark_close.sort_index().pct_change(fill_method=None).dropna(how="all")
    if returns.empty:
        return pd.DataFrame()

    portfolio_weights = _normalize_weights(positions.set_index("ticker")["market_value"]) if not positions.empty else pd.Series(dtype=float)

    # Active Risk(Tracking Error) 계산 시 현금 비중으로 인한 벤치마크와의 괴리 반영
    math_returns = returns.copy()
    if "CASH" in portfolio_weights.index and "CASH" not in math_returns.columns:
        math_returns["CASH"] = 0.0

    symbols = [symbol for symbol in math_returns.columns if symbol in set(benchmark_weights.index).union(set(portfolio_weights.index))]
    if not symbols:
        return pd.DataFrame()
    cov = math_returns[symbols].cov() * 252.0
    active_weights = portfolio_weights.reindex(symbols).fillna(0.0) - benchmark_weights.reindex(symbols).fillna(0.0)
    a = active_weights.to_numpy(dtype=float)
    cov_values = cov.to_numpy(dtype=float)
    te_var = float(a.T @ cov_values @ a)
    if not np.isfinite(te_var) or te_var <= 0:
        return pd.DataFrame()
    te_vol = float(np.sqrt(te_var))
    marginal = cov_values @ a / te_vol
    component = a * marginal
    sector_lookup = dict(zip(sector_frame["Symbol"], sector_frame["Sector"]))
    frame = pd.DataFrame(
        {
            "ticker": symbols,
            "sector": ["Cash" if s == "CASH" else sector_lookup.get(s, "Unknown") for s in symbols],
            "style_bucket": [style_map.get(symbol, "Unknown") for symbol in symbols],
            "portfolio_weight_pct": portfolio_weights.reindex(symbols).fillna(0.0).to_numpy(dtype=float) * 100.0,
            "benchmark_weight_pct": benchmark_weights.reindex(symbols).fillna(0.0).to_numpy(dtype=float) * 100.0,
            "active_weight_pct": active_weights.to_numpy(dtype=float) * 100.0,
            "te_component_pct": component * 100.0,
            "active_risk_contribution_pct": np.where(te_vol > 0, component / te_vol * 100.0, np.nan),
        }
    )
    return frame[frame["active_weight_pct"].abs() > 1e-8].sort_values(
        "active_risk_contribution_pct", ascending=False
    ).reset_index(drop=True)


def _fit_factor_split(asset: pd.Series, market: pd.Series, sector: pd.Series) -> tuple[float, float, float]:
    joined = pd.concat(
        [
            pd.to_numeric(asset, errors="coerce").rename("asset"),
            pd.to_numeric(market, errors="coerce").rename("market"),
            pd.to_numeric(sector, errors="coerce").rename("sector"),
        ],
        axis=1,
    ).dropna()
    if len(joined) < 20:
        return np.nan, np.nan, np.nan
    x_market = joined["market"].to_numpy(dtype=float)
    x_sector_raw = joined["sector"].to_numpy(dtype=float)
    denom = float(np.dot(x_market, x_market))
    beta_sector_market = float(np.dot(x_sector_raw, x_market) / denom) if denom > 0 else 0.0
    x_sector = x_sector_raw - beta_sector_market * x_market
    x = np.column_stack([x_market, x_sector])
    y = joined["asset"].to_numpy(dtype=float)
    beta, _, _, _ = np.linalg.lstsq(x, y, rcond=None)
    fitted = x @ beta
    resid = y - fitted
    market_var = float(np.var(beta[0] * x_market, ddof=1)) if len(y) > 2 else np.nan
    sector_var = float(np.var(beta[1] * x_sector, ddof=1)) if len(y) > 2 else np.nan
    resid_var = float(np.var(resid, ddof=1)) if len(y) > 2 else np.nan
    total = market_var + sector_var + resid_var
    if not np.isfinite(total) or total <= 0:
        return np.nan, np.nan, np.nan
    return market_var / total, sector_var / total, resid_var / total


def _build_factor_risk(
    positions: pd.DataFrame,
    risk_contribution: pd.DataFrame,
    active_risk_contribution: pd.DataFrame,
    holding_returns: pd.DataFrame,
    benchmark_daily: pd.Series,
    sector_returns: pd.DataFrame,
    *,
    style_map: dict[str, str],
) -> pd.DataFrame:
    if positions.empty or risk_contribution.empty or holding_returns.empty or benchmark_daily.empty:
        return pd.DataFrame()
    contribution_map = risk_contribution.set_index("ticker")["risk_contribution_pct"].to_dict()
    active_map = (
        active_risk_contribution.set_index("ticker")["active_risk_contribution_pct"].to_dict()
        if active_risk_contribution is not None and not active_risk_contribution.empty
        else {}
    )
    rows: list[dict[str, object]] = []
    for row in positions.itertuples(index=False):
        ticker = str(row.ticker)
        sector = str(row.sector)

        if ticker == "CASH":
            rows.append({
                "ticker": "CASH",
                "sector": "Cash",
                "style_bucket": "Cash",
                "holding_risk_pct": float(contribution_map.get("CASH", 0.0)),
                "active_risk_pct": float(active_map.get("CASH", 0.0)),
                "market_factor_pct": 0.0,
                "sector_factor_pct": 0.0,
                "specific_factor_pct": 0.0,
                "market_portfolio_risk_pct": 0.0,
                "sector_portfolio_risk_pct": 0.0,
                "specific_portfolio_risk_pct": 0.0,
                "market_active_risk_pct": 0.0,
                "sector_active_risk_pct": 0.0,
                "specific_active_risk_pct": 0.0,
            })
            continue

        if ticker not in holding_returns.columns or sector not in sector_returns.columns:
            continue
        market_share, sector_share, specific_share = _fit_factor_split(
            holding_returns[ticker], benchmark_daily, sector_returns[sector]
        )
        holding_share = float(contribution_map.get(ticker, np.nan))
        rows.append(
            {
                "ticker": ticker,
                "sector": sector,
                "style_bucket": style_map.get(ticker, "Unknown"),
                "holding_risk_pct": holding_share,
                "active_risk_pct": float(active_map.get(ticker, np.nan)),
                "market_factor_pct": market_share * 100.0 if pd.notna(market_share) else np.nan,
                "sector_factor_pct": sector_share * 100.0 if pd.notna(sector_share) else np.nan,
                "specific_factor_pct": specific_share * 100.0 if pd.notna(specific_share) else np.nan,
                "market_portfolio_risk_pct": holding_share * market_share / 100.0 if pd.notna(holding_share) and pd.notna(market_share) else np.nan,
                "sector_portfolio_risk_pct": holding_share * sector_share / 100.0 if pd.notna(holding_share) and pd.notna(sector_share) else np.nan,
                "specific_portfolio_risk_pct": holding_share * specific_share / 100.0 if pd.notna(holding_share) and pd.notna(specific_share) else np.nan,
                "market_active_risk_pct": float(active_map.get(ticker, np.nan)) * market_share / 100.0 if pd.notna(active_map.get(ticker, np.nan)) and pd.notna(market_share) else np.nan,
                "sector_active_risk_pct": float(active_map.get(ticker, np.nan)) * sector_share / 100.0 if pd.notna(active_map.get(ticker, np.nan)) and pd.notna(sector_share) else np.nan,
                "specific_active_risk_pct": float(active_map.get(ticker, np.nan)) * specific_share / 100.0 if pd.notna(active_map.get(ticker, np.nan)) and pd.notna(specific_share) else np.nan,
            }
        )
    return pd.DataFrame(rows).sort_values("holding_risk_pct", ascending=False).reset_index(drop=True) if rows else pd.DataFrame()


def _build_style_exposure(
    positions: pd.DataFrame,
    benchmark_weights: pd.Series,
    sector_frame: pd.DataFrame,
    *,
    style_map: dict[str, str],
) -> pd.DataFrame:
    if benchmark_weights.empty:
        return pd.DataFrame()
    portfolio_weights = _normalize_weights(positions.set_index("ticker")["market_value"]) if not positions.empty else pd.Series(dtype=float)
    symbols = sorted(set(benchmark_weights.index).union(set(portfolio_weights.index)))
    lookup = dict(zip(sector_frame["Symbol"], sector_frame["Sector"]))
    rows: list[dict[str, object]] = []
    for symbol in symbols:
        is_cash = symbol == "CASH"
        rows.append(
            {
                "ticker": symbol,
                "sector": "Cash" if is_cash else lookup.get(symbol, "Unknown"),
                "style_bucket": "Cash" if is_cash else style_map.get(symbol, "Unknown"),
                "portfolio_weight_pct": float(portfolio_weights.get(symbol, 0.0) * 100.0),
                "benchmark_weight_pct": float(benchmark_weights.get(symbol, 0.0) * 100.0),
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    grouped = (
        frame.groupby("style_bucket", as_index=False)
        .agg(
            portfolio_weight_pct=("portfolio_weight_pct", "sum"),
            benchmark_weight_pct=("benchmark_weight_pct", "sum"),
        )
    )
    grouped["active_weight_pct"] = grouped["portfolio_weight_pct"] - grouped["benchmark_weight_pct"]
    return grouped.sort_values("active_weight_pct", ascending=False).reset_index(drop=True)


def _technical_snapshot(close_history: pd.DataFrame) -> pd.DataFrame:
    if close_history.empty:
        return pd.DataFrame(columns=["ticker", "momentum_20d_pct", "momentum_60d_pct", "distance_sma20_pct", "volatility_20d_pct", "technical_score"])
    rows: list[dict[str, object]] = []
    for ticker in close_history.columns:
        series = pd.to_numeric(close_history[ticker], errors="coerce").dropna()
        if len(series) < 5:
            continue
        momentum_20 = _period_return(series, 20) * 100.0
        momentum_60 = _period_return(series, 60) * 100.0 if len(series) > 60 else np.nan
        sma20 = float(series.tail(20).mean())
        latest = float(series.iloc[-1])
        volatility_20 = float(series.pct_change(fill_method=None).tail(20).std(ddof=1) * np.sqrt(252) * 100.0)
        distance_sma20 = ((latest / sma20) - 1.0) * 100.0 if sma20 not in (0.0, np.nan) else np.nan
        score = (
            50.0
            + np.clip(momentum_20, -20.0, 20.0) * 1.2
            + np.clip(momentum_60 if pd.notna(momentum_60) else 0.0, -30.0, 30.0) * 0.6
            + np.clip(distance_sma20, -15.0, 15.0) * 0.8
            - np.clip(volatility_20, 0.0, 60.0) * 0.25
        )
        rows.append(
            {
                "ticker": ticker,
                "momentum_20d_pct": momentum_20,
                "momentum_60d_pct": momentum_60,
                "distance_sma20_pct": distance_sma20,
                "volatility_20d_pct": volatility_20,
                "technical_score": float(np.clip(score, 0.0, 100.0)),
            }
        )
    return pd.DataFrame(
        rows,
        columns=[
            "ticker",
            "momentum_20d_pct",
            "momentum_60d_pct",
            "distance_sma20_pct",
            "volatility_20d_pct",
            "technical_score",
        ],
    )


def _build_scoring(
    positions: pd.DataFrame,
    close_history: pd.DataFrame,
    *,
    shared_db: Path | str | None = None,
    as_of_date: str | pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, str]:
    scoring_columns = [
        "ticker",
        "sector",
        "portfolio_weight_pct",
        "momentum_20d_pct",
        "momentum_60d_pct",
        "distance_sma20_pct",
        "volatility_20d_pct",
        "technical_score",
        "recent_news_count",
        "avg_sentiment_score",
        "news_signal_score",
        "roe",
        "per",
        "pbr",
        "financial_score",
        "integrated_score",
    ]
    if positions.empty:
        return pd.DataFrame(columns=scoring_columns), "financial_metrics:not_available"
    technical = _technical_snapshot(close_history)
    news = _latest_news_signals(positions["ticker"].astype(str).tolist(), shared_db=shared_db)
    financial, financial_source = _load_optional_financial_metrics(
        positions["ticker"].astype(str).tolist(),
        shared_db=shared_db,
        as_of_date=as_of_date,
    )
    frame = positions[["ticker", "sector", "portfolio_weight"]].copy()
    frame = frame.merge(technical, on="ticker", how="left")
    frame = frame.merge(news, on="ticker", how="left")
    frame = frame.merge(financial, on="ticker", how="left")
    frame["financial_score"] = (
        50.0
        + np.where(frame["roe"].notna(), np.clip(frame["roe"] * 100.0, -30.0, 30.0), 0.0)
        - np.where(frame["per"].notna(), np.clip(frame["per"] - 18.0, -20.0, 20.0) * 0.8, 0.0)
        - np.where(frame["pbr"].notna(), np.clip(frame["pbr"] - 3.0, -10.0, 10.0) * 2.0, 0.0)
    ).clip(0.0, 100.0)
    financial_missing = frame[["roe", "per", "pbr"]].isna().all(axis=1)
    frame.loc[financial_missing, "financial_score"] = 50.0
    frame["technical_score"] = frame["technical_score"].fillna(50.0)
    frame["news_signal_score"] = frame["news_signal_score"].fillna(50.0)
    frame["integrated_score"] = (
        frame["technical_score"] * 0.4
        + frame["news_signal_score"] * 0.3
        + frame["financial_score"] * 0.3
    ).clip(0.0, 100.0)
    out = frame.rename(columns={"portfolio_weight": "portfolio_weight_pct"})
    for column in scoring_columns:
        if column not in out.columns:
            out[column] = np.nan
    return out.sort_values(["integrated_score", "portfolio_weight_pct"], ascending=[False, False]).reset_index(drop=True), financial_source


def _apply_weight_constraints(
    base_weights: pd.Series,
    *,
    sector_lookup: dict[str, str],
    sector_cap_pct: float,
    max_position_pct: float,
    cash_buffer_pct: float,
) -> tuple[pd.Series, dict[str, float]]:
    base = pd.to_numeric(base_weights, errors="coerce").fillna(0.0)
    base = base[base > 0].sort_values(ascending=False)
    if base.empty:
        return pd.Series({"CASH": 1.0}), {
            "requested_cash_buffer_pct": float(cash_buffer_pct),
            "actual_cash_weight_pct": 100.0,
            "sector_cap_pct": float(sector_cap_pct),
            "max_position_pct": float(max_position_pct),
        }
    sector_cap = max(float(sector_cap_pct), 0.0) / 100.0
    position_cap = max(float(max_position_pct), 0.0) / 100.0
    cash_target = min(max(float(cash_buffer_pct), 0.0), 95.0) / 100.0
    invested_target = max(0.0, 1.0 - cash_target)
    if invested_target <= 0:
        return pd.Series({"CASH": 1.0}), {
            "requested_cash_buffer_pct": float(cash_buffer_pct),
            "actual_cash_weight_pct": 100.0,
            "sector_cap_pct": float(sector_cap_pct),
            "max_position_pct": float(max_position_pct),
        }
    weights = base / float(base.sum()) * invested_target
    if position_cap > 0:
        weights = weights.clip(upper=position_cap)
    if sector_cap > 0:
        for sector in sorted({sector_lookup.get(symbol, "Unknown") for symbol in weights.index}):
            members = [symbol for symbol in weights.index if sector_lookup.get(symbol, "Unknown") == sector]
            total = float(weights.reindex(members).sum())
            if total > sector_cap and total > 0:
                weights.loc[members] = weights.loc[members] * (sector_cap / total)
    for _ in range(32):
        deficit = invested_target - float(weights.sum())
        if deficit <= 1e-9:
            break
        sector_totals = weights.groupby([sector_lookup.get(symbol, "Unknown") for symbol in weights.index]).sum()
        eligible: list[str] = []
        headroom_name: list[float] = []
        headroom_sector: list[float] = []
        for symbol in weights.index:
            symbol_headroom = max(position_cap - float(weights.loc[symbol]), 0.0) if position_cap > 0 else max(deficit, 0.0)
            sector = sector_lookup.get(symbol, "Unknown")
            sector_headroom_value = max(sector_cap - float(sector_totals.get(sector, 0.0)), 0.0) if sector_cap > 0 else max(deficit, 0.0)
            effective_headroom = min(symbol_headroom, sector_headroom_value)
            if effective_headroom > 1e-12:
                eligible.append(symbol)
                headroom_name.append(symbol_headroom)
                headroom_sector.append(sector_headroom_value)
        if not eligible:
            break
        basis = base.reindex(eligible).fillna(0.0)
        if float(basis.sum()) <= 0:
            basis = pd.Series(1.0, index=eligible)
        proposed = basis / float(basis.sum()) * deficit
        cap_series = pd.Series(
            np.minimum(np.array(headroom_name, dtype=float), np.array(headroom_sector, dtype=float)),
            index=eligible,
        )
        actual_add = pd.concat([proposed.rename("proposed"), cap_series.rename("cap")], axis=1).min(axis=1)
        if float(actual_add.sum()) <= 1e-12:
            break
        weights.loc[eligible] = weights.loc[eligible] + actual_add
    weights = weights[weights > 1e-10].sort_values(ascending=False)
    actual_cash = max(0.0, 1.0 - float(weights.sum()))
    if actual_cash > 1e-10:
        weights.loc["CASH"] = actual_cash
    return weights.sort_values(ascending=False), {
        "requested_cash_buffer_pct": float(cash_buffer_pct),
        "actual_cash_weight_pct": actual_cash * 100.0,
        "sector_cap_pct": float(sector_cap_pct),
        "max_position_pct": float(max_position_pct),
    }


def _allocation_frame_from_weights(
    weights: pd.Series,
    *,
    sector_lookup: dict[str, str],
    annual_return: pd.Series,
    annual_vol: pd.Series,
    score_map: pd.Series,
    current_weights: pd.Series | None = None,
    total_portfolio_value: float = 0.0,
    current_shares: pd.Series | None = None,
    price_map: dict[str, float] | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    # 최적화 대상 종목과 현재 보유 종목의 합집합 티커를 대상으로 합니다.
    all_symbols = sorted(set(weights.index) | set(current_weights.index if current_weights is not None else []))

    for symbol in all_symbols:
        weight = float(weights.get(symbol, 0.0))
        is_cash = str(symbol).upper() == "CASH"
        target_pct = float(weight) * 100.0
        current_pct = 0.0
        if current_weights is not None:
            current_pct = float(current_weights.get(symbol, 0.0))
        
        # 제안 비중과 현재 비중이 모두 0이면 표시할 필요가 없으므로 건너뜁니다.
        if target_pct <= 0 and current_pct <= 0:
            continue

        trade_text = "-"
        if is_cash and total_portfolio_value > 0:
            target_amt = (target_pct / 100.0) * total_portfolio_value
            curr_amt = (current_pct / 100.0) * total_portfolio_value
            diff_amt = target_amt - curr_amt
            if abs(diff_amt) > 1.0: # 1원 이상의 유의미한 차이만 표시
                trade_text = f"{'DEPOSIT' if diff_amt > 0 else 'WITHDRAW'} {abs(diff_amt):,.0f} KRW"

        elif not is_cash and total_portfolio_value > 0 and price_map:
            price = price_map.get(str(symbol), 0.0)
            if price > 0:
                target_qty = int(np.floor((float(weight) * total_portfolio_value) / price))
                curr_qty = float(current_shares.get(symbol, 0.0)) if current_shares is not None else 0.0
                diff_qty = target_qty - curr_qty
                if diff_qty != 0:
                    trade_text = f"{'BUY' if diff_qty > 0 else 'SELL'} {abs(int(diff_qty))}"

        rows.append(
            {
                "ticker": str(symbol),
                "sector": "Cash" if is_cash else sector_lookup.get(str(symbol), "Unknown"),
                "target_weight_pct": target_pct,
                "current_weight_pct": current_pct,
                "diff_weight_pct": target_pct - current_pct,
                "suggested_trade": trade_text,
                "expected_return_pct": 0.0 if is_cash else float(annual_return.reindex([symbol]).fillna(0.0).iloc[0] * 100.0),
                "volatility_pct": 0.0 if is_cash else float(annual_vol.reindex([symbol]).fillna(0.0).iloc[0] * 100.0),
                "integrated_score": np.nan if is_cash else float(score_map.reindex([symbol]).fillna(0.5).iloc[0] * 100.0),
            }
        )
    return pd.DataFrame(rows).sort_values("target_weight_pct", ascending=False).reset_index(drop=True)


def _sector_target_weights(frame: pd.DataFrame) -> pd.Series:
    if frame.empty or "Sector" not in frame.columns:
        return pd.Series(dtype=float)
    working = frame.copy()
    working["Sector"] = working["Sector"].astype(str).str.strip().replace({"": "Unknown", "nan": "Unknown"})
    if "benchmark_weight" in working.columns:
        basis = pd.to_numeric(working["benchmark_weight"], errors="coerce").fillna(0.0)
    elif "market_cap" in working.columns:
        basis = pd.to_numeric(working["market_cap"], errors="coerce").fillna(0.0)
    else:
        basis = pd.Series(1.0, index=working.index)
    sector_weights = basis.groupby(working["Sector"]).sum()
    return _normalize_weights(sector_weights[sector_weights > 0])


def _sector_balanced_universe(
    frame: pd.DataFrame,
    *,
    universe_size: int,
    current_symbols: set[str] | None = None,
) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["Symbol", "Sector", "market_cap", "benchmark_weight"])
    requested = max(int(universe_size), 1)
    current_symbols = {str(symbol).strip().upper() for symbol in (current_symbols or set()) if str(symbol).strip()}
    working = frame.copy()
    working["Symbol"] = working["Symbol"].astype(str).str.strip().str.upper()
    working["Sector"] = working["Sector"].astype(str).str.strip().replace({"": "Unknown", "nan": "Unknown"})
    if "market_cap" not in working.columns:
        working["market_cap"] = 0.0
    if "benchmark_weight" not in working.columns:
        working["benchmark_weight"] = np.nan
    working["market_cap"] = pd.to_numeric(working["market_cap"], errors="coerce").fillna(0.0)
    working["benchmark_weight"] = pd.to_numeric(working["benchmark_weight"], errors="coerce")
    working = working.dropna(subset=["Symbol"]).drop_duplicates(subset=["Symbol"], keep="first")
    working = working.sort_values(["Sector", "market_cap", "Symbol"], ascending=[True, False, True])

    sector_weights = _sector_target_weights(working)
    if sector_weights.empty:
        sector_weights = _normalize_weights(working.groupby("Sector")["market_cap"].sum())
    if sector_weights.empty:
        sector_weights = pd.Series(1.0 / max(working["Sector"].nunique(), 1), index=sorted(working["Sector"].unique()))

    raw_quota = sector_weights * requested
    quotas = np.floor(raw_quota).astype(int)
    for sector in sector_weights.index:
        if sector in set(working["Sector"]) and quotas.get(sector, 0) <= 0:
            quotas.loc[sector] = 1
    while int(quotas.sum()) > requested and len(quotas) > 0:
        reducible = quotas[quotas > 1]
        if reducible.empty:
            break
        sector = (raw_quota.reindex(reducible.index).fillna(0.0) - reducible).idxmin()
        quotas.loc[sector] -= 1
    remainder = requested - int(quotas.sum())
    if remainder > 0:
        fractional = (raw_quota - np.floor(raw_quota)).sort_values(ascending=False)
        for sector in fractional.index:
            if remainder <= 0:
                break
            if sector not in set(working["Sector"]):
                continue
            quotas.loc[sector] = int(quotas.get(sector, 0)) + 1
            remainder -= 1

    selected_parts: list[pd.DataFrame] = []
    for sector, quota in quotas.items():
        if int(quota) <= 0:
            continue
        members = working[working["Sector"].eq(sector)].sort_values(["market_cap", "Symbol"], ascending=[False, True])
        selected_parts.append(members.head(int(quota)))
    selected = pd.concat(selected_parts, ignore_index=True) if selected_parts else working.head(requested).copy()

    missing_current = [symbol for symbol in current_symbols if symbol != "CASH" and symbol not in set(selected["Symbol"])]
    if missing_current:
        selected = pd.concat([selected, working[working["Symbol"].isin(missing_current)]], ignore_index=True)
    if len(selected.index) < requested:
        remaining = working[~working["Symbol"].isin(set(selected["Symbol"]))]
        selected = pd.concat([selected, remaining.sort_values(["market_cap", "Symbol"], ascending=[False, True]).head(requested - len(selected.index))])
    return (
        selected.drop_duplicates(subset=["Symbol"], keep="first")
        .sort_values(["Sector", "market_cap", "Symbol"], ascending=[True, False, True])
        .reset_index(drop=True)
    )


def _allocation_frame_from_share_targets(
    target_shares: pd.Series,
    *,
    sector_lookup: dict[str, str],
    annual_return: pd.Series,
    annual_vol: pd.Series,
    score_map: pd.Series,
    current_weights: pd.Series | None = None,
    total_portfolio_value: float = 0.0,
    current_shares: pd.Series | None = None,
    price_map: dict[str, float] | None = None,
) -> pd.DataFrame:
    price_map = price_map or {}
    rows: list[dict[str, object]] = []
    share_targets = pd.to_numeric(target_shares, errors="coerce").fillna(0.0)
    all_symbols = sorted(set(share_targets.index) | set(current_weights.index if current_weights is not None else []))
    invested_amount = 0.0

    for symbol in all_symbols:
        symbol = str(symbol)
        is_cash = symbol.upper() == "CASH"
        if is_cash:
            continue
        price = float(price_map.get(symbol, 0.0) or 0.0)
        target_qty = int(max(np.floor(float(share_targets.get(symbol, 0.0))), 0))
        curr_qty = float(current_shares.get(symbol, 0.0)) if current_shares is not None else 0.0
        current_pct = float(current_weights.get(symbol, 0.0)) if current_weights is not None else 0.0
        target_amount = target_qty * price
        invested_amount += target_amount
        target_pct = (target_amount / total_portfolio_value * 100.0) if total_portfolio_value > 0 else 0.0
        if target_qty <= 0 and current_pct <= 0 and abs(curr_qty) <= 1e-9:
            continue
        diff_qty = target_qty - int(round(curr_qty))
        trade_text = "-" if diff_qty == 0 else f"{'BUY' if diff_qty > 0 else 'SELL'} {abs(int(diff_qty))}"
        rows.append(
            {
                "ticker": symbol,
                "sector": sector_lookup.get(symbol, "Unknown"),
                "target_shares": int(target_qty),
                "current_shares": int(round(curr_qty)),
                "share_delta": int(diff_qty),
                "target_amount": float(target_amount),
                "target_weight_pct": target_pct,
                "current_weight_pct": current_pct,
                "diff_weight_pct": target_pct - current_pct,
                "suggested_trade": trade_text,
                "expected_return_pct": float(annual_return.reindex([symbol]).fillna(0.0).iloc[0] * 100.0),
                "volatility_pct": float(annual_vol.reindex([symbol]).fillna(0.0).iloc[0] * 100.0),
                "integrated_score": float(score_map.reindex([symbol]).fillna(0.5).iloc[0] * 100.0),
            }
        )

    current_cash_pct = float(current_weights.get("CASH", 0.0)) if current_weights is not None else 0.0
    target_cash = max(float(total_portfolio_value) - invested_amount, 0.0)
    target_cash_pct = (target_cash / total_portfolio_value * 100.0) if total_portfolio_value > 0 else 100.0
    cash_diff = target_cash_pct - current_cash_pct
    rows.append(
        {
            "ticker": "CASH",
            "sector": "Cash",
            "target_shares": int(round(target_cash)),
            "current_shares": int(round((current_cash_pct / 100.0) * total_portfolio_value)) if total_portfolio_value > 0 else 0,
            "share_delta": int(round((cash_diff / 100.0) * total_portfolio_value)) if total_portfolio_value > 0 else 0,
            "target_amount": float(target_cash),
            "target_weight_pct": target_cash_pct,
            "current_weight_pct": current_cash_pct,
            "diff_weight_pct": cash_diff,
            "suggested_trade": "-",
            "expected_return_pct": 0.0,
            "volatility_pct": 0.0,
            "integrated_score": np.nan,
        }
    )
    return pd.DataFrame(rows).sort_values("target_weight_pct", ascending=False).reset_index(drop=True)


def _build_share_targets(
    candidates: pd.DataFrame,
    *,
    sector_targets: pd.Series,
    total_portfolio_value: float,
    cash_buffer_pct: float,
    max_position_pct: float,
    rank_signal: pd.Series,
) -> tuple[pd.Series, dict[str, float]]:
    if candidates.empty or total_portfolio_value <= 0:
        return pd.Series(dtype=float), {"actual_cash_weight_pct": 100.0}
    working = candidates.copy()
    working["Symbol"] = working["Symbol"].astype(str).str.strip().str.upper()
    working["Sector"] = working["Sector"].astype(str).str.strip().replace({"": "Unknown", "nan": "Unknown"})
    working["price"] = pd.to_numeric(working.get("price"), errors="coerce").fillna(0.0)
    working["market_cap"] = pd.to_numeric(working.get("market_cap"), errors="coerce").fillna(0.0)
    working = working[working["price"] > 0].drop_duplicates(subset=["Symbol"], keep="first")
    if working.empty:
        return pd.Series(dtype=float), {"actual_cash_weight_pct": 100.0}

    cash_target = min(max(float(cash_buffer_pct), 0.0), 95.0) / 100.0
    budget = max(float(total_portfolio_value) * (1.0 - cash_target), 0.0)
    position_cap = max(float(max_position_pct), 0.0) / 100.0 * float(total_portfolio_value)
    shares = pd.Series(0, index=working["Symbol"].tolist(), dtype=int)
    spent_by_sector = pd.Series(0.0, index=sorted(working["Sector"].unique()))
    sector_budget = _normalize_weights(sector_targets.reindex(spent_by_sector.index).fillna(0.0))
    if sector_budget.empty or float(sector_budget.sum()) <= 0:
        sector_budget = _normalize_weights(working.groupby("Sector")["market_cap"].sum()).reindex(spent_by_sector.index).fillna(0.0)
    if float(sector_budget.sum()) <= 0:
        sector_budget = pd.Series(1.0 / len(spent_by_sector.index), index=spent_by_sector.index)
    sector_budget = sector_budget / float(sector_budget.sum()) * budget

    rank = pd.to_numeric(rank_signal, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0)
    working["rank_signal"] = working["Symbol"].map(rank).fillna(0.0)
    if float(working["rank_signal"].clip(lower=0.0).sum()) <= 0:
        working["rank_signal"] = working["market_cap"]
    working["rank_signal"] = working["rank_signal"].clip(lower=0.0)

    for sector, sector_rows in working.groupby("Sector"):
        target_budget = float(sector_budget.get(sector, 0.0))
        if target_budget <= 0:
            continue
        sector_rows = sector_rows.sort_values(["rank_signal", "market_cap", "Symbol"], ascending=[False, False, True])
        basis = sector_rows["rank_signal"].where(sector_rows["rank_signal"] > 0, sector_rows["market_cap"])
        basis = basis.clip(lower=0.0)
        if float(basis.sum()) <= 0:
            basis = pd.Series(1.0, index=sector_rows.index)
        for idx, row in sector_rows.iterrows():
            symbol = str(row["Symbol"])
            price = float(row["price"])
            position_limit = position_cap if position_cap > 0 else target_budget
            desired = target_budget * float(basis.loc[idx]) / float(basis.sum())
            qty = int(np.floor(min(desired, position_limit) / price))
            if qty > 0:
                shares.loc[symbol] += qty
                spent_by_sector.loc[sector] += qty * price

    def _can_add(row: pd.Series, total_spent: float) -> bool:
        symbol = str(row["Symbol"])
        price = float(row["price"])
        position_limit = position_cap if position_cap > 0 else budget
        if total_spent + price > budget:
            return False
        return (float(shares.get(symbol, 0)) + 1.0) * price <= position_limit + 1e-9

    total_spent = float((shares.reindex(working["Symbol"]).fillna(0).to_numpy() * working["price"].to_numpy()).sum())
    min_price = float(working["price"].min())
    for _ in range(max(len(working.index) * 8, 1)):
        if total_spent + min_price > budget:
            break
        scored = working.copy()
        scored["sector_gap"] = scored["Sector"].map(sector_budget - spent_by_sector).fillna(0.0)
        scored = scored[scored["sector_gap"] > 0].sort_values(
            ["sector_gap", "rank_signal", "market_cap", "Symbol"],
            ascending=[False, False, False, True],
        )
        added = False
        for _, row in scored.iterrows():
            if not _can_add(row, total_spent):
                continue
            symbol = str(row["Symbol"])
            price = float(row["price"])
            shares.loc[symbol] += 1
            sector = str(row["Sector"])
            spent_by_sector.loc[sector] += price
            total_spent += price
            added = True
            break
        if not added:
            break

    actual_cash = max(float(total_portfolio_value) - total_spent, 0.0) / float(total_portfolio_value) * 100.0
    return shares[shares > 0].astype(int), {
        "requested_cash_buffer_pct": float(cash_buffer_pct),
        "actual_cash_weight_pct": actual_cash,
        "max_position_pct": float(max_position_pct),
    }


def _forecast_return_proxy(close_history: pd.DataFrame, ticker: str, horizon_days: int) -> float:
    if ticker not in close_history.columns:
        return 0.0
    series = pd.to_numeric(close_history[ticker], errors="coerce").dropna()
    if len(series) < 40:
        return 0.0
    momentum_20 = _period_return(series, 20)
    momentum_60 = _period_return(series, 60) if len(series) > 60 else momentum_20
    volatility = float(series.pct_change(fill_method=None).tail(20).std(ddof=1))
    annualized = (0.65 * np.nan_to_num(momentum_20, nan=0.0) + 0.35 * np.nan_to_num(momentum_60, nan=0.0))
    scaled = annualized * (max(int(horizon_days), 1) / 20.0)
    damped = scaled / max(1.0, volatility * np.sqrt(252) * 6.0)
    return float(np.clip(damped, -0.15, 0.15))


def build_portfolio_dashboard(
    *,
    portfolio_db: Path | str | None = None,
    portfolio_user_id: str | None = None,
    shared_db: Path | str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    start_date: str | None = None,
    end_date: str | None = None,
) -> PortfolioDashboard:
    lookback = max(int(lookback_days), 21)
    trades = load_trades(portfolio_db)
    sector_map, component_source = _sector_map(max_symbols=0)
    positions_raw = _current_positions_from_trades(trades, sector_map=sector_map)
    if positions_raw.empty:
        return PortfolioDashboard(
            as_of_date=None,
            trades=trades,
            positions=positions_raw,
            holdings_performance=pd.DataFrame(),
            portfolio_summary=_build_portfolio_summary(positions_raw, pd.DataFrame(), pd.Series(dtype=float), pd.Series(dtype=float), as_of_date=None),
            attribution=pd.DataFrame(),
            stock_attribution=pd.DataFrame(),
            style_attribution=pd.DataFrame(),
            risk_summary=pd.DataFrame(),
            relative_risk_summary=pd.DataFrame(),
            risk_contribution=pd.DataFrame(),
            active_risk_contribution=pd.DataFrame(),
            factor_risk=pd.DataFrame(),
            style_exposure=pd.DataFrame(),
            scoring=pd.DataFrame(),
            diagnostics={
                "portfolio_db": str(portfolio_db_path(portfolio_db).resolve()),
                "shared_db": str(_shared_db_path(shared_db).resolve()),
                "component_source": component_source,
                "price_source": "not_used",
            },
        )

    # 현금 잔고 마이너스 체크
    cash_warning = ""
    cash_row = positions_raw[positions_raw["ticker"] == "CASH"]
    if not cash_row.empty and float(cash_row.iloc[0]["net_quantity"]) < 0:
        cash_warning = f"경고: 현재 현금 잔고가 마이너스({abs(float(cash_row.iloc[0]['net_quantity'])):,.0f} KRW)입니다. 초과 매수 상태를 점검하세요."

    # 1. 분석 기준일 결정 (Read-only)
    db_max_ts = _get_db_max_date(shared_db)

    # GUI에서 '종료일'이 오늘(2026-04-21)이거나 더 미래로 설정된 경우, DB의 마지막 데이터 날짜(4월 17일)로 스냅합니다.
    if end_date:
        req_end = pd.Timestamp(end_date).normalize()
        end_ts = db_max_ts if req_end >= pd.Timestamp.today().normalize() else req_end
    else:
        end_ts = db_max_ts

    # 시작일 설정: GUI 입력값을 우선하되, 없으면 lookback을 적용합니다.
    if start_date:
        start_ts = pd.Timestamp(start_date).normalize()
    else:
        start_ts = end_ts - pd.Timedelta(days=lookback * 2)

    # CASH 티커는 외부 데이터(yfinance) 호출에서 제외
    tickers = [t for t in positions_raw["ticker"].astype(str).tolist() if t != "CASH"]
    close_history, _, position_sources = _load_close_history(
        tickers,
        start_date=start_ts.strftime("%Y-%m-%d"),
        end_date=end_ts.strftime("%Y-%m-%d"),
        shared_db=shared_db,
    )
    positions, as_of_date = _build_positions_frame(positions_raw, close_history=close_history)
    holdings_performance = _build_holdings_performance(positions, close_history)
    portfolio_daily = _portfolio_return_series(close_history, positions)

    sector_frame, _ = _load_component_frame(max_symbols=0)
    universe = sector_frame["Symbol"].astype(str).tolist()
    benchmark_frame, benchmark_component_source = _load_kospi200_benchmark_frame(shared_db=shared_db)
    benchmark_symbols = benchmark_frame["Symbol"].astype(str).tolist() if not benchmark_frame.empty else universe
    benchmark_sector_frame = benchmark_frame[["Symbol", "Sector"]].copy() if not benchmark_frame.empty else sector_frame
    benchmark_close, benchmark_caps, benchmark_sources = _load_close_history(
        benchmark_symbols,
        start_date=start_ts.strftime("%Y-%m-%d"),
        end_date=end_ts.strftime("%Y-%m-%d"),
        shared_db=shared_db,
    )
    explicit_benchmark_weights = (
        benchmark_frame.set_index("Symbol")["benchmark_weight"]
        if not benchmark_frame.empty and "benchmark_weight" in benchmark_frame.columns
        else None
    )
    benchmark_daily, sector_returns, benchmark_weights = _benchmark_series(
        close_history=benchmark_close,
        market_caps=benchmark_caps,
        sector_frame=benchmark_sector_frame,
        benchmark_weights=explicit_benchmark_weights,
    )
    style_map, _, style_source = _build_style_map(universe, shared_db=shared_db, as_of_date=end_ts)
    positions["style_bucket"] = positions["ticker"].map(style_map).fillna("Unknown")
    holdings_performance["style_bucket"] = holdings_performance["ticker"].map(style_map).fillna("Unknown")
    stock_attribution, attribution, style_attribution = _build_relative_attribution_tables(
        positions,
        close_history,
        benchmark_close,
        benchmark_weights,
        benchmark_sector_frame,
        style_map=style_map,
    )
    holding_returns = _holding_returns(close_history)
    risk_summary, risk_contribution, relative_risk_summary = _build_risk_summary(
        positions, holding_returns, portfolio_daily, benchmark_daily
    )
    active_risk_contribution = _build_active_risk_contribution(
        positions,
        benchmark_weights,
        benchmark_close,
        benchmark_sector_frame,
        style_map=style_map,
    )
    factor_risk = _build_factor_risk(
        positions,
        risk_contribution,
        active_risk_contribution,
        holding_returns,
        benchmark_daily,
        sector_returns,
        style_map=style_map,
    )
    style_exposure = _build_style_exposure(positions, benchmark_weights, benchmark_sector_frame, style_map=style_map)

    # --- 통합 스코어링 확장 로직 (KRX 전체 대상) ---
    sector_lookup = dict(zip(sector_frame["Symbol"], sector_frame["Sector"]))
    universe_input = pd.DataFrame({
        "ticker": universe,
        "sector": [sector_lookup.get(s, "Unknown") for s in universe],
        "portfolio_weight": 0.0
    })
    full_universe_scoring, financial_source = _build_scoring(
        universe_input,
        benchmark_close,
        shared_db=shared_db,
        as_of_date=end_ts,
    )
    
    # 1. 보유 종목 스코어링 (기존 상세 내역용)
    held_tickers = set(positions_raw[positions_raw["ticker"] != "CASH"]["ticker"])
    scoring = full_universe_scoring[full_universe_scoring["ticker"].isin(held_tickers)].copy()
    weight_map = positions.set_index("ticker")["portfolio_weight"].to_dict()
    scoring["portfolio_weight_pct"] = scoring["ticker"].map(weight_map).fillna(0.0)
    scoring = scoring.sort_values(["integrated_score", "portfolio_weight_pct"], ascending=[False, False]).reset_index(drop=True)

    # 2. 미보유 종목 중 상위 10개 추천
    top_recommendations = full_universe_scoring[~full_universe_scoring["ticker"].isin(held_tickers)].copy()
    top_recommendations = top_recommendations.sort_values("integrated_score", ascending=False).head(10).reset_index(drop=True)

    if not scoring.empty:
        scoring["style_bucket"] = scoring["ticker"].map(style_map).fillna("Unknown")

    # 시각화 차트 생성
    cum_chart = _build_cumulative_return_chart(portfolio_daily, benchmark_daily)
    sector_chart = _build_sector_contribution_chart(attribution)
    style_chart = _build_style_exposure_chart(style_exposure)

    # 1. 벤치마크 차트를 먼저 생성하여 기준이 되는 섹터 순서를 가져옵니다.
    bench_alloc_chart, bench_order = _build_benchmark_sector_allocation_chart(benchmark_weights, benchmark_sector_frame)
    # 2. 내 포트폴리오 차트 생성 시 벤치마크의 정렬 순서를 주입합니다.
    alloc_chart = _build_sector_allocation_chart(positions, sector_order=bench_order)

    # 통합 스코어링 시각화 및 요약 생성
    integrated_score_chart = _build_integrated_score_chart(scoring if not scoring.empty else full_universe_scoring)
    display_cols = ["ticker", "sector", "integrated_score", "technical_score", "news_signal_score", "financial_score"]
    best_scoring_stocks = scoring[display_cols].head(10).copy() if not scoring.empty else pd.DataFrame()
    worst_scoring_stocks = scoring[display_cols].tail(10).iloc[::-1].copy() if not scoring.empty else pd.DataFrame()
    scoring_commentary = _build_scoring_commentary(scoring, top_recommendations)

    # 리스크 시각화 차트 생성
    risk_contrib_chart = _build_risk_contribution_chart(risk_contribution)
    active_risk_chart = _build_active_risk_contribution_chart(active_risk_contribution)

    portfolio_summary = _build_portfolio_summary(
        positions, 
        holdings_performance, 
        portfolio_daily, 
        benchmark_daily, 
        as_of_date=as_of_date,
        start_ts=start_ts,
        end_ts=end_ts
    )
    diagnostics = {
        "portfolio_db": str(portfolio_db_path(portfolio_db).resolve()),
        "shared_db": str(_shared_db_path(shared_db).resolve()),
        "component_source": component_source,
        "benchmark_component_source": benchmark_component_source,
        "price_source": position_sources.get("price_source", "sqlite"),
        "benchmark_price_source": benchmark_sources.get("price_source", "sqlite"),
        "financial_metric_source": financial_source,
        "style_source": style_source,
        "cash_warning": cash_warning,
    }
    return PortfolioDashboard(
        as_of_date=as_of_date,
        trades=trades,
        positions=positions,
        holdings_performance=holdings_performance,
        portfolio_summary=portfolio_summary,
        attribution=attribution,
        stock_attribution=stock_attribution,
        style_attribution=style_attribution,
        risk_summary=risk_summary,
        relative_risk_summary=relative_risk_summary,
        risk_contribution=risk_contribution,
        active_risk_contribution=active_risk_contribution,
        factor_risk=factor_risk,
        style_exposure=style_exposure,
        scoring=scoring,
        cumulative_chart=cum_chart,
        sector_contribution_chart=sector_chart,
        style_exposure_chart=style_chart,
        sector_allocation_chart=alloc_chart,
        benchmark_sector_allocation_chart=bench_alloc_chart,
        integrated_score_chart=integrated_score_chart,
        best_scoring_stocks=best_scoring_stocks,
        worst_scoring_stocks=worst_scoring_stocks,
        top_recommendations=top_recommendations,
        scoring_commentary=scoring_commentary,
        risk_contribution_chart=risk_contrib_chart,
        active_risk_contribution_chart=active_risk_chart,
        diagnostics=diagnostics,
    )


def analyze_virtual_trade(
    *,
    ticker: str,
    side: str,
    quantity: float,
    price: float | None = None,
    fees: float = 0.0,
    portfolio_db: Path | str | None = None,
    shared_db: Path | str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    forecast_horizon_days: int = DEFAULT_VIRTUAL_FORECAST_HORIZON_DAYS,
) -> VirtualTradeResult:
    ticker_clean = resolve_security_symbol(ticker, shared_db=shared_db)
    side_clean = str(side or "").strip().upper()
    if side_clean not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    qty_value = float(quantity)
    if qty_value <= 0:
        raise ValueError("quantity must be positive")
    before = build_portfolio_dashboard(portfolio_db=portfolio_db, shared_db=shared_db, lookback_days=lookback_days)
    db_now = _get_db_max_date(shared_db)
    trades = before.trades.copy()
    end_ts = db_now
    start_ts = end_ts - pd.Timedelta(days=max(int(lookback_days), 21) * 2)
    close_history, _, sources = _load_close_history(
        sorted(set(before.positions["ticker"].astype(str).tolist() + [ticker_clean])),
        start_date=start_ts.strftime("%Y-%m-%d"),
        end_date=end_ts.strftime("%Y-%m-%d"),
        shared_db=shared_db,
    )
    inferred_price = float(close_history[ticker_clean].dropna().iloc[-1]) if ticker_clean in close_history.columns and not close_history[ticker_clean].dropna().empty else np.nan
    trade_price = float(price) if price is not None else inferred_price
    if not np.isfinite(trade_price) or trade_price <= 0:
        raise ValueError("price could not be resolved from the shared DB")
    forecast_return = _forecast_return_proxy(close_history, ticker_clean, forecast_horizon_days)
    forecast_price = trade_price * (1.0 + forecast_return)
    synthetic = pd.DataFrame(
        [
            {
                "id": int((trades["id"].max() + 1) if not trades.empty else 1),
                "trade_date": end_ts,
                "ticker": ticker_clean,
                "side": side_clean,
                "quantity": qty_value,
                "price": trade_price,
                "fees": float(fees),
                "notes": f"virtual_trade_{forecast_horizon_days}d",
                "created_at": end_ts.strftime("%Y-%m-%d %H:%M:%S"),
                "gross_amount": qty_value * trade_price,
                "net_cash_flow": -(qty_value * trade_price + float(fees))
                if side_clean == "BUY"
                else (qty_value * trade_price - float(fees)),
            }
        ]
    )
    combined = pd.concat([trades, synthetic], ignore_index=True) if not trades.empty else synthetic
    sector_map, component_source = _sector_map(max_symbols=500)
    positions_after_raw = _current_positions_from_trades(combined, sector_map=sector_map)
    positions_after, as_of_date = _build_positions_frame(positions_after_raw, close_history=close_history)
    holdings_after = _build_holdings_performance(positions_after, close_history)

    # 가상 거래 후 현금 잔고 체크
    cash_warning = ""
    cash_row_after = positions_after_raw[positions_after_raw["ticker"] == "CASH"]
    if not cash_row_after.empty and float(cash_row_after.iloc[0]["net_quantity"]) < 0:
        cash_warning = f"가상 거래 경고: 실행 시 예상 현금 잔고가 마이너스({abs(float(cash_row_after.iloc[0]['net_quantity'])):,.0f} KRW)가 됩니다."

    portfolio_after = _portfolio_return_series(close_history, positions_after)

    sector_frame, _ = _load_component_frame(max_symbols=500)
    benchmark_close, benchmark_caps, _ = _load_close_history(
        sector_frame["Symbol"].astype(str).tolist(),
        start_date=start_ts.strftime("%Y-%m-%d"),
        end_date=end_ts.strftime("%Y-%m-%d"),
        shared_db=shared_db,
    )
    benchmark_daily, _, _ = _benchmark_series(
        close_history=benchmark_close,
        market_caps=benchmark_caps,
        sector_frame=sector_frame,
    )
    after_summary = _build_portfolio_summary(
        positions_after, 
        holdings_after, 
        portfolio_after, 
        benchmark_daily, 
        as_of_date=as_of_date,
        start_ts=start_ts,
        end_ts=end_ts
    )
    before_summary = before.portfolio_summary.copy()
    before_positions = before.positions[["ticker", "market_value", "portfolio_weight", "net_quantity"]].rename(
        columns={
            "market_value": "before_market_value",
            "portfolio_weight": "before_weight_pct",
            "net_quantity": "before_quantity",
        }
    )
    after_positions = positions_after[["ticker", "market_value", "portfolio_weight", "net_quantity"]].rename(
        columns={
            "market_value": "after_market_value",
            "portfolio_weight": "after_weight_pct",
            "net_quantity": "after_quantity",
        }
    )
    changes = before_positions.merge(after_positions, on="ticker", how="outer").fillna(0.0)
    changes["market_value_delta"] = changes["after_market_value"] - changes["before_market_value"]
    changes["weight_delta_pct"] = changes["after_weight_pct"] - changes["before_weight_pct"]
    changes["quantity_delta"] = changes["after_quantity"] - changes["before_quantity"]
    changes = changes.sort_values("after_market_value", ascending=False).reset_index(drop=True)

    before_risk = before.risk_summary.copy()
    after_risk, _, _ = _build_risk_summary(
        positions_after,
        _holding_returns(close_history),
        portfolio_after,
        benchmark_daily,
    )
    risk_changes = pd.DataFrame()
    if not before_risk.empty and not after_risk.empty:
        for col in [c for c in after_risk.columns if c in before_risk.columns]:
            risk_changes.loc[0, f"before_{col}"] = before_risk.iloc[0][col]
            risk_changes.loc[0, f"after_{col}"] = after_risk.iloc[0][col]
            risk_changes.loc[0, f"delta_{col}"] = after_risk.iloc[0][col] - before_risk.iloc[0][col]
    input_summary = pd.DataFrame(
        [
            {
                "ticker": ticker_clean,
                "side": side_clean,
                "quantity": qty_value,
                "trade_price": trade_price,
                "forecast_horizon_days": int(forecast_horizon_days),
                "forecast_return_pct": forecast_return * 100.0,
                "forecast_price": forecast_price,
            }
        ]
    )
    diagnostics = {
        "component_source": component_source,
        "price_source": sources.get("price_source", "sqlite"),
        "cash_warning": cash_warning,
    }
    return VirtualTradeResult(
        input_summary=input_summary,
        before_summary=before_summary,
        after_summary=after_summary,
        position_changes=changes,
        risk_changes=risk_changes,
        diagnostics=diagnostics,
    )


def build_portfolio_optimization(
    *,
    portfolio_db: Path | str | None = None,
    portfolio_user_id: str | None = None,
    shared_db: Path | str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    start_date: str | None = None,
    end_date: str | None = None,
    universe_size: int = DEFAULT_OPTIMIZATION_UNIVERSE_SIZE,
    sector_cap_pct: float = DEFAULT_SECTOR_CAP_PCT,
    max_position_pct: float = DEFAULT_MAX_POSITION_PCT,
    cash_buffer_pct: float = DEFAULT_CASH_BUFFER_PCT,
) -> OptimizationResult:
    dashboard = build_portfolio_dashboard(portfolio_db=portfolio_db, shared_db=shared_db, lookback_days=lookback_days)
    current_symbols = set(dashboard.positions["ticker"].astype(str)) if not dashboard.positions.empty and "ticker" in dashboard.positions.columns else set()
    benchmark_frame, benchmark_source = _load_kospi200_benchmark_frame(shared_db=shared_db, max_symbols=max(int(universe_size), 200))
    if benchmark_frame.empty:
        universe_basis, component_source = _load_component_frame(max_symbols=0)
        component_source = f"{component_source}:sector_balanced"
    else:
        universe_basis = benchmark_frame.copy()
        component_source = f"{benchmark_source}:sector_balanced"
    cap_lookup = _latest_market_caps_for_symbols(universe_basis["Symbol"].astype(str).tolist(), shared_db=shared_db)
    universe_basis["market_cap"] = universe_basis["Symbol"].astype(str).str.upper().map(cap_lookup).fillna(0.0)
    sector_targets = _sector_target_weights(universe_basis)
    sector_frame = _sector_balanced_universe(
        universe_basis,
        universe_size=max(int(universe_size), 1),
        current_symbols=current_symbols,
    )
    sector_lookup = dict(zip(sector_frame["Symbol"], sector_frame["Sector"]))
    missing_symbols = {
        symbol
        for symbol in current_symbols
        if str(symbol).upper() != "CASH" and str(symbol) not in sector_lookup
    }
    if missing_symbols:
        full_sector_frame, _ = _load_component_frame(max_symbols=0)
        full_sector_lookup = dict(zip(full_sector_frame["Symbol"], full_sector_frame["Sector"]))
        sector_lookup.update({symbol: full_sector_lookup[symbol] for symbol in missing_symbols if symbol in full_sector_lookup})
        extras = full_sector_frame[full_sector_frame["Symbol"].isin(missing_symbols)].copy()
        if not extras.empty:
            extra_caps = _latest_market_caps_for_symbols(extras["Symbol"].astype(str).tolist(), shared_db=shared_db)
            extras["market_cap"] = extras["Symbol"].map(extra_caps).fillna(0.0)
            sector_frame = pd.concat([sector_frame, extras], ignore_index=True).drop_duplicates(subset=["Symbol"], keep="first")
    db_now = _get_db_max_date(shared_db)
    universe = sector_frame["Symbol"].astype(str).tolist()
    end_ts = db_now
    start_ts = end_ts - pd.Timedelta(days=max(int(lookback_days), 21) * 2)
    close_history, market_caps, sources = _load_close_history(
        universe,
        start_date=start_ts.strftime("%Y-%m-%d"),
        end_date=end_ts.strftime("%Y-%m-%d"),
        shared_db=shared_db,
    )
    if close_history.empty:
        empty = pd.DataFrame()
        diagnostics = pd.DataFrame(
            [{"metric": "status", "value": "shared price history unavailable"}, {"metric": "component_source", "value": component_source}]
        )
        return OptimizationResult(empty, empty, empty, diagnostics)
    returns = _holding_returns(close_history).dropna(how="all", axis=1)
    if returns.empty:
        empty = pd.DataFrame()
        diagnostics = pd.DataFrame([{"metric": "status", "value": "insufficient return history"}])
        return OptimizationResult(empty, empty, empty, diagnostics)
    latest_caps = market_caps.ffill().iloc[-1].reindex(returns.columns) if not market_caps.empty else pd.Series(dtype=float)
    cap_weights = _normalize_weights(latest_caps) if not latest_caps.empty else pd.Series(1.0 / len(returns.columns), index=returns.columns)
    
    # 1. 먼저 통합 스코어 계산 (기대수익률 보정에 사용하기 위해 순서 조정)
    scoring_input_df = pd.DataFrame(
        {
            "ticker": returns.columns,
            "sector": [sector_lookup.get(ticker, "Unknown") for ticker in returns.columns],
            "portfolio_weight": np.repeat(100.0 / len(returns.columns), len(returns.columns)),
        }
    )
    scores, financial_source = _build_scoring(
        scoring_input_df,
        close_history[returns.columns],
        shared_db=shared_db,
        as_of_date=end_ts,
    )
    score_map = (
        pd.to_numeric(scores.set_index("ticker")["integrated_score"], errors="coerce")
        .reindex(returns.columns)
        .astype(float)
        .fillna(50.0)
        / 100.0
    )

    # 2. 멀티팩터 기대수익률(Expected Return) 산출
    # CAGR(연복리수익률) 기반 기술적 추세: 단순 산술평균의 Volatility Drag 왜곡을 방지하기 위해 기하평균(로그수익률 연율화) 사용
    log_returns = np.log(1.0 + returns.fillna(0)).replace([np.inf, -np.inf], 0)
    historical_return = np.exp(log_returns.mean() * 252) - 1.0
    
    # 통합 스코어 보정(Alpha Tilt): 점수가 50점(0.5) 기준 상하로 최대 +-15%p 범위에서 수익률 기대치를 보정
    score_tilt = (score_map - 0.5) * 0.30
    annual_return = historical_return + score_tilt

    annual_vol = returns.std(ddof=1) * np.sqrt(252)
    
    total_val = float(dashboard.portfolio_summary.iloc[0]["market_value"]) if not dashboard.portfolio_summary.empty else 0.0
    current_weights = dashboard.positions.set_index("ticker")["portfolio_weight"] if not dashboard.positions.empty else pd.Series(dtype=float)
    current_shares = dashboard.positions.set_index("ticker")["net_quantity"] if not dashboard.positions.empty else pd.Series(dtype=float)
    price_map = close_history.ffill().iloc[-1].to_dict() if not close_history.empty else {}
    allocation_candidates = sector_frame[sector_frame["Symbol"].isin(returns.columns)].copy()
    allocation_candidates["price"] = allocation_candidates["Symbol"].map(price_map)
    allocation_candidates["market_cap"] = allocation_candidates["Symbol"].map(latest_caps).fillna(
        pd.to_numeric(allocation_candidates.get("market_cap"), errors="coerce").fillna(0.0)
    )

    def _calc_port_impact(w_series: pd.Series):
        w_aligned = w_series.reindex(returns.columns).fillna(0.0)
        if w_aligned.sum() > 0: w_aligned = w_aligned / w_aligned.sum()
        p_ret = (w_aligned * annual_return.reindex(w_aligned.index).fillna(0.0)).sum()
        p_vol = np.sqrt(w_aligned.T @ (returns.cov() * 252) @ w_aligned)
        return {"ret": p_ret * 100.0, "vol": p_vol * 100.0}

    def _weights_from_allocation(df: pd.DataFrame) -> pd.Series:
        if df.empty or "ticker" not in df.columns or "target_weight_pct" not in df.columns:
            return pd.Series(dtype=float)
        frame = df[df["ticker"].astype(str).str.upper().ne("CASH")].copy()
        return pd.to_numeric(frame.set_index("ticker")["target_weight_pct"], errors="coerce").fillna(0.0) / 100.0

    curr_w_norm = (current_weights / 100.0).reindex(returns.columns).fillna(0.0)
    impact_curr = _calc_port_impact(curr_w_norm)

    replication_shares, replication_summary = _build_share_targets(
        allocation_candidates,
        sector_targets=sector_targets,
        total_portfolio_value=total_val,
        cash_buffer_pct=cash_buffer_pct,
        max_position_pct=max_position_pct,
        rank_signal=cap_weights,
    )
    def _finalize_opt_df(df: pd.DataFrame) -> pd.DataFrame:
        """현재 보유 종목과 최적화 제안 종목을 모두 포함하여 반환합니다."""
        if df.empty: return df
        return df.sort_values("target_weight_pct", ascending=False).reset_index(drop=True)

    replication_all = _allocation_frame_from_share_targets(
        replication_shares,
        sector_lookup=sector_lookup,
        annual_return=annual_return,
        annual_vol=annual_vol,
        score_map=score_map,
        current_weights=current_weights,
        total_portfolio_value=total_val,
        current_shares=current_shares,
        price_map=price_map,
    )
    replication = _finalize_opt_df(replication_all)
    impact_rep = _calc_port_impact(_weights_from_allocation(replication))

    aggressive_signal = np.maximum(annual_return.fillna(0.0), 0.0) * (0.6 + score_map * 0.8) / annual_vol.replace(0.0, np.nan)
    aggressive_shares, aggressive_summary = _build_share_targets(
        allocation_candidates,
        sector_targets=sector_targets,
        total_portfolio_value=total_val,
        cash_buffer_pct=cash_buffer_pct,
        max_position_pct=max_position_pct,
        rank_signal=_normalize_weights(aggressive_signal.replace([np.inf, -np.inf], np.nan).fillna(0.0)),
    )
    aggressive_all = _allocation_frame_from_share_targets(
        aggressive_shares,
        sector_lookup=sector_lookup,
        annual_return=annual_return,
        annual_vol=annual_vol,
        score_map=score_map,
        current_weights=current_weights,
        total_portfolio_value=total_val,
        current_shares=current_shares,
        price_map=price_map,
    )
    aggressive = _finalize_opt_df(aggressive_all)
    impact_agg = _calc_port_impact(_weights_from_allocation(aggressive))

    defensive_signal = (score_map + 0.35) / annual_vol.replace(0.0, np.nan)
    defensive_shares, defensive_summary = _build_share_targets(
        allocation_candidates,
        sector_targets=sector_targets,
        total_portfolio_value=total_val,
        cash_buffer_pct=cash_buffer_pct,
        max_position_pct=max_position_pct,
        rank_signal=_normalize_weights(defensive_signal.replace([np.inf, -np.inf], np.nan).fillna(0.0)),
    )
    defensive_all = _allocation_frame_from_share_targets(
        defensive_shares,
        sector_lookup=sector_lookup,
        annual_return=annual_return,
        annual_vol=annual_vol,
        score_map=score_map,
        current_weights=current_weights,
        total_portfolio_value=total_val,
        current_shares=current_shares,
        price_map=price_map,
    )
    defensive = _finalize_opt_df(defensive_all)
    impact_def = _calc_port_impact(_weights_from_allocation(defensive))

    impact_df = pd.DataFrame([
        {"구분": "기대 수익률(연 %)", "현재": impact_curr["ret"], "복제": impact_rep["ret"], "공격": impact_agg["ret"], "방어": impact_def["ret"]},
        {"구분": "예상 변동성(연 %)", "현재": impact_curr["vol"], "복제": impact_rep["vol"], "공격": impact_agg["vol"], "방어": impact_def["vol"]},
        {"구분": "샤프 지수", "현재": impact_curr["ret"]/impact_curr["vol"] if impact_curr["vol"]>0 else 0, 
                          "복제": impact_rep["ret"]/impact_rep["vol"] if impact_rep["vol"]>0 else 0,
                          "공격": impact_agg["ret"]/impact_agg["vol"] if impact_agg["vol"]>0 else 0,
                          "방어": impact_def["ret"]/impact_def["vol"] if impact_def["vol"]>0 else 0}
    ])

    diagnostics = pd.DataFrame(
        [
            {"metric": "component_source", "value": component_source},
            {"metric": "price_source", "value": sources.get("price_source", "sqlite")},
            {"metric": "financial_metric_source", "value": financial_source},
            {"metric": "current_portfolio_holdings", "value": str(len(dashboard.positions.index))},
            {"metric": "optimization_universe", "value": str(len(returns.columns))},
            {"metric": "sector_cap_pct", "value": f"{sector_cap_pct:.2f}"},
            {"metric": "max_position_pct", "value": f"{max_position_pct:.2f}"},
            {"metric": "requested_cash_buffer_pct", "value": f"{cash_buffer_pct:.2f}"},
            {"metric": "replication_actual_cash_pct", "value": f"{replication_summary['actual_cash_weight_pct']:.2f}"},
            {"metric": "aggressive_actual_cash_pct", "value": f"{aggressive_summary['actual_cash_weight_pct']:.2f}"},
            {"metric": "defensive_actual_cash_pct", "value": f"{defensive_summary['actual_cash_weight_pct']:.2f}"},
        ]
    )

    # 리밸런싱 대조 차트 생성
    rep_chart = _build_rebalancing_chart(dashboard.positions, replication, "Trade: Current vs KRX Replication")
    agg_chart = _build_rebalancing_chart(dashboard.positions, aggressive, "Trade: Current vs Aggressive")
    def_chart = _build_rebalancing_chart(dashboard.positions, defensive, "Trade: Current vs Defensive")

    return OptimizationResult(
        replication=replication, aggressive=aggressive, defensive=defensive, diagnostics=diagnostics,
        replication_chart=rep_chart, aggressive_chart=agg_chart, defensive_chart=def_chart,
        impact_summary=impact_df
    )


def analyze_virtual_trades(
    *,
    trade_requests: list[dict[str, object]] | pd.DataFrame,
    portfolio_db: Path | str | None = None,
    portfolio_user_id: str | None = None,
    shared_db: Path | str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    start_date: str | None = None,
    end_date: str | None = None,
    forecast_horizon_days: int = DEFAULT_VIRTUAL_FORECAST_HORIZON_DAYS,
) -> VirtualTradeResult:
    requests = trade_requests.to_dict(orient="records") if isinstance(trade_requests, pd.DataFrame) else list(trade_requests or [])
    if not requests:
        raise ValueError("trade_requests must include at least one trade")

    result: VirtualTradeResult | None = None
    for request in requests:
        raw_price = request.get("price")
        result = analyze_virtual_trade(
            ticker=str(request.get("ticker") or ""),
            side=str(request.get("side") or ""),
            quantity=float(request.get("quantity") or 0),
            price=float(raw_price) if raw_price not in {None, ""} else None,
            fees=float(request.get("fees") or 0),
            portfolio_db=portfolio_db,
            shared_db=shared_db,
            lookback_days=lookback_days,
            forecast_horizon_days=forecast_horizon_days,
        )
    if result is None:
        raise ValueError("trade_requests must include at least one trade")
    return result
