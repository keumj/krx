from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from pipeline_common.news_data import NewsArticle, fetch_google_news_articles, fetch_naver_news_articles
from pipeline_common.security import configure_ssl, security_hint

from .db import init_krx_project_db, krx_prices_sqlite_path


DEFAULT_COMPONENTS_CSV = Path("data/krx_components_full.csv")
DEFAULT_BACKFILL_DAYS = 32
DEFAULT_INCREMENTAL_OVERLAP_DAYS = 2
DEFAULT_MAX_ITEMS = 30
DEFAULT_PROGRESS_BATCH_SIZE = 200
DEFAULT_NEWS_PROVIDER = "google"
_CANCEL_REQUESTED = False


@dataclass(frozen=True)
class KRXNewsSyncResult:
    db_path: Path
    components_path: Path
    as_of_date: date
    start_date: date
    symbol_count: int
    inserted_rows: int
    skipped_duplicates: int
    fetch_failures: int


def _log(message: str) -> None:
    print(f"[refresh-krx-news] {message}", flush=True)


def _batch_progress_log(
    *,
    batch_start_index: int,
    batch_end_index: int,
    total_symbols: int,
    articles_seen: int | None = None,
    window_matches: int | None = None,
    inserted_rows: int | None = None,
    skipped_duplicates: int | None = None,
    fetch_failures: int | None = None,
    batch_inserted_rows: int | None = None,
    batch_skipped_duplicates: int | None = None,
    batch_fetch_failures: int | None = None,
    note: str | None = None,
) -> None:
    parts = [f"{batch_start_index}-{batch_end_index}/{total_symbols}"]
    if note:
        parts.append(str(note))
    if articles_seen is not None:
        parts.append(f"articles={int(articles_seen)}")
    if window_matches is not None:
        parts.append(f"window_matches={int(window_matches)}")
    if batch_inserted_rows is not None:
        parts.append(f"inserted_batch={int(batch_inserted_rows)}")
    if batch_skipped_duplicates is not None:
        parts.append(f"duplicates_batch={int(batch_skipped_duplicates)}")
    if batch_fetch_failures is not None:
        parts.append(f"failures_batch={int(batch_fetch_failures)}")
    if inserted_rows is not None:
        parts.append(f"inserted_total={int(inserted_rows)}")
    if skipped_duplicates is not None:
        parts.append(f"duplicates_total={int(skipped_duplicates)}")
    if fetch_failures is not None:
        parts.append(f"failures_total={int(fetch_failures)}")
    _log(" ".join(parts))


def _handle_sigint(_signum: int, _frame: object) -> None:
    global _CANCEL_REQUESTED
    _CANCEL_REQUESTED = True
    raise KeyboardInterrupt


def _raise_if_cancelled() -> None:
    if _CANCEL_REQUESTED:
        raise KeyboardInterrupt


def _load_krx_news_targets(components_csv: Path) -> list[dict[str, str]]:
    raw = pd.read_csv(components_csv)
    if raw.empty:
        return []
    cols = {str(c).strip().lower(): c for c in raw.columns}
    symbol_col = cols.get("symbol") or raw.columns[0]
    name_kr_col = cols.get("namekr") or cols.get("name_kr") or cols.get("name")
    name_en_col = cols.get("nameen") or cols.get("name_en")

    targets: list[dict[str, str]] = []
    seen: set[str] = set()
    for record in raw.to_dict(orient="records"):
        symbol = str(record.get(symbol_col) or "").strip().upper()
        if symbol.isdigit():
            symbol = symbol.zfill(6)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        name_kr = str(record.get(name_kr_col) or "").strip() if name_kr_col is not None else ""
        name_en = str(record.get(name_en_col) or "").strip() if name_en_col is not None else ""
        targets.append(
            {
                "symbol": symbol,
                "name_kr": name_kr,
                "name_en": name_en,
            }
        )
    return targets


def _filter_targets_by_existing_news_count(
    conn: sqlite3.Connection,
    targets: list[dict[str, str]],
    *,
    start_date: date,
    as_of_date: date,
    max_existing_articles: int,
) -> list[dict[str, str]]:
    threshold = max(int(max_existing_articles), 0)
    if threshold <= 0 or not targets:
        return targets
    symbols = [str(item.get("symbol") or "").strip().upper() for item in targets if str(item.get("symbol") or "").strip()]
    if not symbols:
        return []
    counts: dict[str, int] = {}
    chunk_size = 900
    for offset in range(0, len(symbols), chunk_size):
        chunk = symbols[offset : offset + chunk_size]
        placeholders = ",".join(["?"] * len(chunk))
        rows = conn.execute(
            f"""
            SELECT symbol, COUNT(*)
            FROM news_articles
            WHERE date(publish_date) BETWEEN ? AND ?
              AND symbol IN ({placeholders})
            GROUP BY symbol
            """,
            (start_date.isoformat(), as_of_date.isoformat(), *chunk),
        ).fetchall()
        counts.update({str(symbol): int(count) for symbol, count in rows})
    return [item for item in targets if counts.get(str(item.get("symbol") or "").strip().upper(), 0) < threshold]


def _coerce_date(value: str | date | datetime | None, *, fallback: date) -> date:
    if value is None:
        return fallback
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return pd.Timestamp(value).date()


def _article_is_in_window(article: NewsArticle, *, start_date: date, as_of_date: date) -> bool:
    if article.publish_date is None:
        return False
    article_day = article.publish_date.date()
    return start_date <= article_day <= as_of_date


def _effective_news_start_date(
    conn: sqlite3.Connection,
    *,
    as_of_date: date,
    backfill_days: int,
    overlap_days: int,
) -> date:
    row = conn.execute("SELECT MAX(publish_date) FROM news_articles").fetchone()
    max_publish_text = str(row[0] or "").strip() if row else ""
    if not max_publish_text:
        return as_of_date - timedelta(days=max(backfill_days - 1, 0))
    max_publish_date = pd.Timestamp(max_publish_text).date()
    start_date = max_publish_date - timedelta(days=max(overlap_days, 0))
    if start_date > as_of_date:
        return as_of_date
    return start_date


def _article_query_for_target(target: dict[str, str]) -> str:
    name_kr = str(target.get("name_kr") or "").strip()
    name_en = str(target.get("name_en") or "").strip()
    if name_kr:
        return f'"{name_kr}" 주식'
    if name_en:
        return f'"{name_en}" stock'
    return f'"{target["symbol"]}" 주식'


def _publish_date_text(value: datetime) -> str:
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _insert_article_if_new(
    conn: sqlite3.Connection,
    *,
    symbol: str,
    provider_query: str,
    article: NewsArticle,
) -> bool:
    if article.publish_date is None:
        return False
    before = conn.total_changes
    conn.execute(
        """
        INSERT INTO news_articles (
            symbol, publish_date, title, summary, link, source, language, provider_query, sentiment_score, analysis_status
        )
        SELECT ?, ?, ?, NULL, ?, ?, 'ko', ?, NULL, 'pending'
        WHERE NOT EXISTS (
            SELECT 1
            FROM news_articles
            WHERE symbol = ? AND link = ?
        )
        """,
        (
            symbol,
            _publish_date_text(article.publish_date),
            article.title,
            article.link,
            article.source,
            provider_query,
            symbol,
            article.link,
        ),
    )
    return conn.total_changes > before


def sync_krx_news_articles(
    *,
    as_of_date: str | date | datetime | None = None,
    start_date: str | date | datetime | None = None,
    backfill_days: int = DEFAULT_BACKFILL_DAYS,
    overlap_days: int = DEFAULT_INCREMENTAL_OVERLAP_DAYS,
    max_items: int = DEFAULT_MAX_ITEMS,
    timeout: int = 8,
    components_csv: Path | str = DEFAULT_COMPONENTS_CSV,
    db_path: Path | str | None = None,
    provider: str = DEFAULT_NEWS_PROVIDER,
    naver_client_id: str | None = None,
    naver_client_secret: str | None = None,
    request_delay: float = 0.0,
    google_hl: str = "en-US",
    google_gl: str = "US",
    google_ceid: str = "US:en",
    max_existing_articles: int = 0,
) -> KRXNewsSyncResult:
    today = datetime.now().date()
    run_date = _coerce_date(as_of_date, fallback=today)
    components_path = Path(components_csv)
    sqlite_result = init_krx_project_db(db_path=Path(db_path) if db_path is not None else None)
    target = sqlite_result.db_path if db_path is not None else krx_prices_sqlite_path()

    targets = _load_krx_news_targets(components_path)
    inserted_rows = 0
    skipped_duplicates = 0
    fetch_failures = 0
    sample_errors: list[str] = []
    provider_name = str(provider or DEFAULT_NEWS_PROVIDER).strip().lower()
    if provider_name not in {"google", "naver"}:
        raise ValueError("provider must be one of: google, naver")
    naver_id = str(naver_client_id or os.getenv("NAVER_NEWS_CLIENT_ID") or os.getenv("NAVER_CLIENT_ID") or "").strip()
    naver_secret = str(
        naver_client_secret or os.getenv("NAVER_NEWS_CLIENT_SECRET") or os.getenv("NAVER_CLIENT_SECRET") or ""
    ).strip()
    if provider_name == "naver" and (not naver_id or not naver_secret):
        raise ValueError("Naver provider requires NAVER_NEWS_CLIENT_ID and NAVER_NEWS_CLIENT_SECRET")

    with sqlite3.connect(target) as conn:
        explicit_start_date = _coerce_date(start_date, fallback=run_date) if start_date is not None else None
        effective_start_date = explicit_start_date or _effective_news_start_date(
            conn,
            as_of_date=run_date,
            backfill_days=backfill_days,
            overlap_days=overlap_days,
        )
        if effective_start_date > run_date:
            effective_start_date = run_date
        if max_existing_articles > 0:
            before_count = len(targets)
            targets = _filter_targets_by_existing_news_count(
                conn,
                targets,
                start_date=effective_start_date,
                as_of_date=run_date,
                max_existing_articles=max_existing_articles,
            )
            _log(
                f"Filtered low-coverage targets: before={before_count}, after={len(targets)}, "
                f"max_existing_articles={int(max_existing_articles)}"
            )
        _log(
            f"Starting KRX news sync: symbols={len(targets)}, as_of_date={run_date.isoformat()}, "
            f"start_date={effective_start_date.isoformat()}, max_items={max_items}, provider={provider_name}"
        )
        total_symbols = len(targets)
        batch_size = DEFAULT_PROGRESS_BATCH_SIZE
        batch_start_index = 1
        batch_articles_seen = 0
        batch_window_matches = 0
        batch_inserted_rows = 0
        batch_skipped_duplicates = 0
        batch_fetch_failures = 0

        for index, item in enumerate(targets, start=1):
            _raise_if_cancelled()
            if index == batch_start_index:
                _batch_progress_log(
                    batch_start_index=batch_start_index,
                    batch_end_index=min(batch_start_index + batch_size - 1, total_symbols),
                    total_symbols=total_symbols,
                    inserted_rows=inserted_rows,
                    skipped_duplicates=skipped_duplicates,
                    fetch_failures=fetch_failures,
                    note="batch_started",
                )
            provider_query = _article_query_for_target(item)
            try:
                if provider_name == "naver":
                    articles = fetch_naver_news_articles(
                        query=provider_query,
                        client_id=naver_id,
                        client_secret=naver_secret,
                        max_items=max_items,
                        timeout=timeout,
                    )
                else:
                    articles = fetch_google_news_articles(
                        query=provider_query,
                        max_items=max_items,
                        timeout=timeout,
                        hl=google_hl,
                        gl=google_gl,
                        ceid=google_ceid,
                    )
            except Exception as exc:
                fetch_failures += 1
                batch_fetch_failures += 1
                if len(sample_errors) < 5:
                    sample_errors.append(f"{item['symbol']}: {type(exc).__name__}: {exc}")
                articles = []
            finally:
                if request_delay > 0:
                    time.sleep(float(request_delay))
            if not articles:
                continue
            window_matches = 0
            batch_articles_seen += len(articles)
            for article in articles:
                if not _article_is_in_window(article, start_date=effective_start_date, as_of_date=run_date):
                    continue
                window_matches += 1
                batch_window_matches += 1
                if _insert_article_if_new(
                    conn,
                    symbol=str(item["symbol"]),
                    provider_query=provider_query,
                    article=article,
                ):
                    inserted_rows += 1
                    batch_inserted_rows += 1
                else:
                    skipped_duplicates += 1
                    batch_skipped_duplicates += 1
            if index % batch_size == 0 or index == total_symbols:
                _batch_progress_log(
                    batch_start_index=batch_start_index,
                    batch_end_index=index,
                    total_symbols=total_symbols,
                    articles_seen=batch_articles_seen,
                    window_matches=batch_window_matches,
                    inserted_rows=inserted_rows,
                    skipped_duplicates=skipped_duplicates,
                    fetch_failures=fetch_failures,
                    batch_inserted_rows=batch_inserted_rows,
                    batch_skipped_duplicates=batch_skipped_duplicates,
                    batch_fetch_failures=batch_fetch_failures,
                    note="batch_done",
                )
                batch_start_index = index + 1
                batch_articles_seen = 0
                batch_window_matches = 0
                batch_inserted_rows = 0
                batch_skipped_duplicates = 0
                batch_fetch_failures = 0
        conn.commit()

    if sample_errors:
        _log("Sample fetch errors:")
        for item in sample_errors:
            _log(f"  {item}")
    if fetch_failures == len(targets) and targets:
        raise RuntimeError(
            "All KRX news fetches failed. Check TLS/SSL settings or outbound access to news.google.com."
        )

    return KRXNewsSyncResult(
        db_path=target,
        components_path=components_path,
        as_of_date=run_date,
        start_date=effective_start_date,
        symbol_count=len(targets),
        inserted_rows=inserted_rows,
        skipped_duplicates=skipped_duplicates,
        fetch_failures=fetch_failures,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backfill and incrementally sync KRX news into SQLite.")
    parser.add_argument("--as-of-date", default=None, help="Cutoff date in YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--start-date", default=None, help="Force a one-time backfill start date in YYYY-MM-DD.")
    parser.add_argument("--backfill-days", type=int, default=DEFAULT_BACKFILL_DAYS, help="Initial one-time lookback window in days.")
    parser.add_argument("--overlap-days", type=int, default=DEFAULT_INCREMENTAL_OVERLAP_DAYS, help="Overlap window in days for incremental reruns.")
    parser.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS, help="Max Google News RSS items per symbol fetch.")
    parser.add_argument("--timeout", type=int, default=8, help="HTTP timeout in seconds.")
    parser.add_argument("--components-csv", default=str(DEFAULT_COMPONENTS_CSV), help="Path to KRX components CSV.")
    parser.add_argument("--db-path", default=None, help="Optional SQLite path override.")
    parser.add_argument("--provider", choices=["google", "naver"], default=DEFAULT_NEWS_PROVIDER, help="News search provider.")
    parser.add_argument("--naver-client-id", default="", help="Naver Search API client ID. Defaults to NAVER_NEWS_CLIENT_ID.")
    parser.add_argument("--naver-client-secret", default="", help="Naver Search API client secret. Defaults to NAVER_NEWS_CLIENT_SECRET.")
    parser.add_argument("--request-delay", type=float, default=0.0, help="Seconds to wait after each provider request.")
    parser.add_argument("--google-hl", default="en-US", help="Google News RSS hl parameter.")
    parser.add_argument("--google-gl", default="US", help="Google News RSS gl parameter.")
    parser.add_argument("--google-ceid", default="US:en", help="Google News RSS ceid parameter.")
    parser.add_argument(
        "--max-existing-articles",
        type=int,
        default=0,
        help="Only fetch symbols with fewer than this many existing news rows in the requested window.",
    )
    parser.add_argument("--insecure-ssl", action="store_true", help="Disable TLS verification for temporary testing")
    parser.add_argument("--ca-bundle", default="", help="Custom CA bundle path")
    return parser


def main(argv: list[str] | None = None) -> int:
    global _CANCEL_REQUESTED
    parser = _build_parser()
    args = parser.parse_args(argv)
    _CANCEL_REQUESTED = False
    previous_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, _handle_sigint)
    try:
        configure_ssl(insecure_ssl=bool(args.insecure_ssl), ca_bundle=str(args.ca_bundle).strip() or None)
        result = sync_krx_news_articles(
            as_of_date=args.as_of_date,
            start_date=args.start_date,
            backfill_days=args.backfill_days,
            overlap_days=args.overlap_days,
            max_items=args.max_items,
            timeout=args.timeout,
            components_csv=args.components_csv,
            db_path=args.db_path,
            provider=args.provider,
            naver_client_id=args.naver_client_id,
            naver_client_secret=args.naver_client_secret,
            request_delay=max(float(args.request_delay), 0.0),
            google_hl=str(args.google_hl or "en-US"),
            google_gl=str(args.google_gl or "US"),
            google_ceid=str(args.google_ceid or "US:en"),
            max_existing_articles=max(int(args.max_existing_articles), 0),
        )
    except KeyboardInterrupt:
        _log("Cancelled by user (Ctrl+C).")
        return 130
    except Exception as exc:
        hint = security_hint(exc, output_dir=Path("data"))
        if hint:
            print(hint, file=sys.stderr)
        _log(f"SUMMARY status=error error_type={type(exc).__name__}")
        _log(f"ERROR {type(exc).__name__}: {exc}")
        return 1
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    _log(
        "SUMMARY status=ok "
        f"synced_news_articles={result.inserted_rows} "
        f"skipped_duplicates={result.skipped_duplicates} "
        f"fetch_failures={result.fetch_failures} "
        f"symbols={result.symbol_count} "
        f"start_date={result.start_date.isoformat()} "
        f"as_of_date={result.as_of_date.isoformat()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
