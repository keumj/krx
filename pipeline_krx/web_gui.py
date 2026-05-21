from __future__ import annotations

import html
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

from .db import load_latest_krx_benchmark_snapshot


_APP_TITLE = "KRX Data Refresh"
_DEFAULT_DB_PATH = Path("data/krx_shared_db/krx_shared_prices.sqlite")
_DEFAULT_CONFIG_PATH = Path("data/krx_web_gui_config.json")
_DEFAULT_COMPONENTS_PATH = Path("data/krx_components_full.csv")
_DEFAULT_UNKNOWN_PATH = Path("data/krx_components_unknown_sectors.csv")
_DEFAULT_CLOSE_CSV_PATH = Path("data/krx_close_prices.csv")
_DEFAULT_MARKET_CAP_CSV_PATH = Path("data/krx_market_caps.csv")
_DEFAULT_SHARES_CSV_PATH = Path("data/krx_shares.csv")
_DEFAULT_KOSPI200_SOURCE_CSV_PATH = Path("data/krx_kospi200_manual.csv")
_DEFAULT_KOSPI200_EXPORT_CSV_PATH = Path("data/krx_kospi200_latest.csv")
_REFRESH_STAGE_DEFS: tuple[dict[str, object], ...] = (
    {
        "step_id": "components",
        "label": "KRX components",
        "title": "종목 마스터",
        "description": "KRX 상장 종목, 시장 구분, 섹터 보정, Unknown 리뷰 파일을 갱신합니다.",
        "item_datasets": ("krx_components_full", "krx_unknown_sectors"),
    },
    {
        "step_id": "prices",
        "label": "KRX prices",
        "title": "가격/시총/주식수",
        "description": "종가, 시가총액, 상장주식수 CSV와 SQLite prices 테이블을 갱신하고 EPS를 보정합니다.",
        "item_datasets": ("krx_close_prices", "krx_market_caps", "krx_shares", "krx_prices_sqlite"),
    },
    {
        "step_id": "fundamentals",
        "label": "KRX DART fundamentals",
        "title": "DART 재무",
        "description": "DART 기준 분기 재무를 SQLite fundamentals_quarterly에 적재하고 주식수 기반 EPS를 갱신합니다.",
        "item_datasets": ("krx_fundamentals_quarterly",),
    },
    {
        "step_id": "news",
        "label": "KRX news",
        "title": "뉴스",
        "description": "KRX 종목명을 기준으로 Google News RSS를 수집해 SQLite news_articles를 갱신합니다.",
        "item_datasets": ("krx_news_articles",),
    },
)


def _project_root_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def _config_path(root_dir: Path) -> Path:
    return root_dir / _DEFAULT_CONFIG_PATH


def _default_ca_bundle_path(root_dir: Path) -> str:
    candidate = root_dir / "data" / "certs" / "windows_root_bundle.pem"
    return str(candidate) if candidate.exists() and candidate.is_file() else ""


def _mask_secret(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return f"{text[:4]}...{text[-4:]}"


def _browser_target_host(host: str) -> str:
    clean = str(host or "").strip()
    return "127.0.0.1" if clean in {"", "0.0.0.0", "::"} else clean


def _schedule_browser_open(*, host: str, port: int) -> None:
    def _runner() -> None:
        try:
            webbrowser.open(f"http://{_browser_target_host(host)}:{int(port)}/refresh")
        except Exception:
            return

    threading.Timer(0.8, _runner).start()


def _load_gui_config(root_dir: Path) -> dict[str, object]:
    path = _config_path(root_dir)
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_gui_config(root_dir: Path, payload: dict[str, object]) -> None:
    path = _config_path(root_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_saved_api_key(root_dir: Path) -> str:
    return str(_load_gui_config(root_dir).get("dart_api_key", "")).strip()


def _delete_saved_api_key(root_dir: Path) -> None:
    config = _load_gui_config(root_dir)
    config.pop("dart_api_key", None)
    _save_gui_config(root_dir, config)


def _sanitize_form_for_state(form: dict[str, str]) -> dict[str, str]:
    keep = dict(form)
    keep["dart_api_key"] = ""
    for key in ("run_components", "run_prices", "run_fundamentals", "run_news", "save_api_key", "use_saved_api_key", "insecure_ssl"):
        keep[key] = "on" if keep.get(key, "") == "on" else ""
    return keep


def _sanitize_benchmark_form_for_state(form: dict[str, str]) -> dict[str, str]:
    keep = dict(form)
    keep["benchmark_source_mode"] = keep.get("benchmark_source_mode", "pykrx_index") or "pykrx_index"
    return keep


def _refresh_stage_defs() -> list[dict[str, object]]:
    return [dict(item) for item in _REFRESH_STAGE_DEFS]


def _refresh_stage_def(step_id: str) -> dict[str, object] | None:
    for item in _REFRESH_STAGE_DEFS:
        if str(item.get("step_id")) == str(step_id):
            return dict(item)
    return None


def _refresh_step_id_from_label(label: str) -> str | None:
    for item in _REFRESH_STAGE_DEFS:
        if str(item.get("label")) == str(label):
            return str(item.get("step_id"))
    return None


def _empty_refresh_stage_states() -> dict[str, dict[str, object]]:
    states: dict[str, dict[str, object]] = {}
    for item in _REFRESH_STAGE_DEFS:
        step_id = str(item["step_id"])
        states[step_id] = {
            "step_id": step_id,
            "label": str(item["label"]),
            "title": str(item["title"]),
            "description": str(item["description"]),
            "status": "idle",
            "selected": False,
            "started_at": None,
            "finished_at": None,
            "logs": [],
            "updated_items": [],
            "log_count": 0,
            "latest_items": [],
            "latest_summary": "",
        }
    return states


def _default_form(root_dir: Path) -> dict[str, str]:
    config = _load_gui_config(root_dir)
    current_year = datetime.now().year
    return {
        "db_path": str(config.get("db_path") or _DEFAULT_DB_PATH),
        "start_date": str(config.get("start_date") or "2019-12-31"),
        "start_year": str(config.get("start_year") or "2019"),
        "end_year": str(config.get("end_year") or str(current_year)),
        "pause_seconds": str(config.get("pause_seconds") or "0.0"),
        "ca_bundle_path": str(config.get("ca_bundle_path") or _default_ca_bundle_path(root_dir)),
        "insecure_ssl": "on" if str(config.get("insecure_ssl") or "") == "on" else "",
        "run_components": "on" if str(config.get("run_components") or "on") == "on" else "",
        "run_prices": "on" if str(config.get("run_prices") or "on") == "on" else "",
        "run_fundamentals": "on" if str(config.get("run_fundamentals") or "on") == "on" else "",
        "run_news": "on" if str(config.get("run_news") or "on") == "on" else "",
        "save_api_key": "",
        "use_saved_api_key": "on" if _read_saved_api_key(root_dir) else "",
        "dart_api_key": "",
    }


def _default_benchmark_form(root_dir: Path) -> dict[str, str]:
    config = _load_gui_config(root_dir)
    return {
        "benchmark_db_path": str(config.get("benchmark_db_path") or config.get("db_path") or _DEFAULT_DB_PATH),
        "benchmark_as_of_date": str(config.get("benchmark_as_of_date") or ""),
        "benchmark_source_mode": str(config.get("benchmark_source_mode") or "pykrx_index"),
        "benchmark_source_csv": str(config.get("benchmark_source_csv") or _DEFAULT_KOSPI200_SOURCE_CSV_PATH),
        "benchmark_export_csv": str(config.get("benchmark_export_csv") or _DEFAULT_KOSPI200_EXPORT_CSV_PATH),
        "benchmark_constituent_count": str(config.get("benchmark_constituent_count") or "200"),
    }


def _read_text_rows(path: Path, *, date_column: str | None = None) -> tuple[int, str | None]:
    if not path.exists() or not path.is_file():
        return 0, None
    try:
        frame = pd.read_csv(path)
    except Exception:
        return 0, None
    latest = None
    if date_column and date_column in frame.columns and not frame.empty:
        date_series = pd.to_datetime(frame[date_column], errors="coerce").dropna()
        if not date_series.empty:
            latest = pd.Timestamp(date_series.max()).strftime("%Y-%m-%d")
    return len(frame.index), latest


def _collect_refresh_items(root_dir: Path, db_path: Path) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    components_rows, _ = _read_text_rows(root_dir / _DEFAULT_COMPONENTS_PATH)
    if components_rows:
        items.append(
            {
                "dataset": "krx_components_full",
                "latest_date": None,
                "rows": components_rows,
                "source": "fdr+desc",
                "path": str(root_dir / _DEFAULT_COMPONENTS_PATH),
            }
        )

    unknown_rows, _ = _read_text_rows(root_dir / _DEFAULT_UNKNOWN_PATH)
    if (root_dir / _DEFAULT_UNKNOWN_PATH).exists():
        items.append(
            {
                "dataset": "krx_unknown_sectors",
                "latest_date": None,
                "rows": unknown_rows,
                "source": "review_csv",
                "path": str(root_dir / _DEFAULT_UNKNOWN_PATH),
            }
        )

    close_rows, close_latest = _read_text_rows(root_dir / _DEFAULT_CLOSE_CSV_PATH, date_column="Date")
    if close_rows:
        items.append(
            {
                "dataset": "krx_close_prices",
                "latest_date": close_latest,
                "rows": close_rows,
                "source": "wide_csv",
                "path": str(root_dir / _DEFAULT_CLOSE_CSV_PATH),
            }
        )

    market_cap_rows, market_cap_latest = _read_text_rows(root_dir / _DEFAULT_MARKET_CAP_CSV_PATH, date_column="Date")
    if market_cap_rows:
        items.append(
            {
                "dataset": "krx_market_caps",
                "latest_date": market_cap_latest,
                "rows": market_cap_rows,
                "source": "wide_csv",
                "path": str(root_dir / _DEFAULT_MARKET_CAP_CSV_PATH),
            }
        )

    shares_rows, shares_latest = _read_text_rows(root_dir / _DEFAULT_SHARES_CSV_PATH, date_column="Date")
    if shares_rows:
        items.append(
            {
                "dataset": "krx_shares",
                "latest_date": shares_latest,
                "rows": shares_rows,
                "source": "wide_csv",
                "path": str(root_dir / _DEFAULT_SHARES_CSV_PATH),
            }
        )

    if db_path.exists() and db_path.is_file():
        try:
            with sqlite3.connect(db_path) as conn:
                prices_rows = int(conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0])
                prices_latest = conn.execute("SELECT MAX(date) FROM prices").fetchone()[0]
                fund_rows = int(conn.execute("SELECT COUNT(*) FROM fundamentals_quarterly").fetchone()[0])
                fund_latest = conn.execute("SELECT MAX(fiscal_date) FROM fundamentals_quarterly").fetchone()[0]
                news_rows = int(conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0])
                news_latest = conn.execute("SELECT MAX(publish_date) FROM news_articles").fetchone()[0]
        except Exception:
            prices_rows = 0
            prices_latest = None
            fund_rows = 0
            fund_latest = None
            news_rows = 0
            news_latest = None
        items.append(
            {
                "dataset": "krx_prices_sqlite",
                "latest_date": prices_latest,
                "rows": prices_rows,
                "source": "sqlite",
                "path": str(db_path),
            }
        )
        items.append(
            {
                "dataset": "krx_fundamentals_quarterly",
                "latest_date": fund_latest,
                "rows": fund_rows,
                "source": "sqlite",
                "path": str(db_path),
            }
        )
        items.append(
            {
                "dataset": "krx_news_articles",
                "latest_date": news_latest,
                "rows": news_rows,
                "source": "sqlite",
                "path": str(db_path),
            }
        )
        benchmark_frame = load_latest_krx_benchmark_snapshot("KOSPI200", db_path=db_path)
        if not benchmark_frame.empty:
            benchmark_latest = str(benchmark_frame["as_of_date"].iloc[0])
            items.append(
                {
                    "dataset": "krx_kospi200_benchmark",
                    "latest_date": benchmark_latest,
                    "rows": len(benchmark_frame.index),
                    "source": str(benchmark_frame["source"].iloc[0] or "unknown"),
                    "path": str(root_dir / _DEFAULT_KOSPI200_EXPORT_CSV_PATH),
                }
            )
    return items


def _collect_benchmark_snapshot_items(db_path: Path) -> list[dict[str, object]]:
    frame = load_latest_krx_benchmark_snapshot("KOSPI200", db_path=db_path)
    if frame.empty:
        return []
    preview = frame.copy()
    preview["benchmark_weight"] = pd.to_numeric(preview["benchmark_weight"], errors="coerce").fillna(0.0)
    items: list[dict[str, object]] = []
    for record in preview.to_dict(orient="records"):
        items.append(
            {
                "member_order": int(record.get("member_order") or 0),
                "symbol": str(record.get("symbol") or ""),
                "name_kr": str(record.get("name_kr") or ""),
                "market": str(record.get("market") or ""),
                "sector": str(record.get("sector") or ""),
                "benchmark_weight": float(record.get("benchmark_weight") or 0.0),
                "as_of_date": str(record.get("as_of_date") or ""),
                "source": str(record.get("source") or ""),
            }
        )
    return items


def _filter_refresh_items_for_step(items: list[dict[str, object]], step_id: str) -> list[dict[str, object]]:
    stage = _refresh_stage_def(step_id)
    if stage is None:
        return []
    dataset_names = {str(value) for value in tuple(stage.get("item_datasets", ()))}
    return [dict(item) for item in items if str(item.get("dataset") or "") in dataset_names]


def _summarize_refresh_items(items: list[dict[str, object]]) -> str:
    if not items:
        return "현재 DB 상태를 확인할 수 없습니다."
    parts: list[str] = []
    for item in items:
        dataset = str(item.get("dataset") or "-")
        latest = str(item.get("latest_date") or "-")
        rows = item.get("rows")
        rows_text = f"{int(rows):,d}" if isinstance(rows, int) else str(rows or "-")
        parts.append(f"{dataset}: latest={latest}, rows={rows_text}")
    return " / ".join(parts)


def _build_refresh_steps(
    *,
    root_dir: Path,
    form: dict[str, str],
    api_key: str | None,
) -> list[tuple[str, str, list[str], dict[str, str]]]:
    db_path = str(Path(form.get("db_path", "")).expanduser())
    ca_bundle = str(form.get("ca_bundle_path", "")).strip()
    insecure_ssl = form.get("insecure_ssl", "") == "on"
    pause_seconds = str(form.get("pause_seconds", "0.0") or "0.0").strip() or "0.0"
    steps: list[tuple[str, str, list[str], dict[str, str]]] = []

    def _common_flags() -> list[str]:
        flags: list[str] = []
        if insecure_ssl:
            flags.append("--insecure-ssl")
        elif ca_bundle:
            flags.extend(["--ca-bundle", ca_bundle])
        return flags

    if form.get("run_components", "") == "on":
        command = [
            sys.executable,
            "-u",
            "-m",
            "pipeline_krx.components",
            "--db-path",
            db_path,
        ]
        command.extend(_common_flags())
        steps.append(("components", "KRX components", command, {}))

    if form.get("run_prices", "") == "on":
        command = [
            sys.executable,
            "-u",
            "-m",
            "pipeline_krx.refresh_prices",
            "--db-path",
            db_path,
            "--start-date",
            str(form.get("start_date", "2019-12-31") or "2019-12-31"),
            "--pause-seconds",
            pause_seconds,
        ]
        command.extend(_common_flags())
        steps.append(("prices", "KRX prices", command, {}))

    if form.get("run_fundamentals", "") == "on":
        command = [
            sys.executable,
            "-u",
            "-m",
            "pipeline_krx.refresh_dart_auto_fundamentals",
            "--db-path",
            db_path,
            "--end-year",
            str(form.get("end_year", str(datetime.now().year)) or str(datetime.now().year)),
            "--overlap-years",
            "1",
            "--pause-seconds",
            pause_seconds,
        ]
        command.extend(_common_flags())
        env = {"KEUMJ_DART_API_KEY": str(api_key or "").strip()} if api_key else {}
        steps.append(("fundamentals", "KRX DART fundamentals", command, env))
    if form.get("run_news", "") == "on":
        command = [
            sys.executable,
            "-u",
            "-m",
            "pipeline_krx.refresh_news",
            "--db-path",
            db_path,
        ]
        command.extend(_common_flags())
        steps.append(("news", "KRX news", command, {}))
    return steps


def _build_benchmark_command(*, root_dir: Path, form: dict[str, str]) -> list[str]:
    command = [
        sys.executable,
        "-u",
        "-m",
        "pipeline_krx.benchmark",
        "--db-path",
        str(Path(form.get("benchmark_db_path", str(_DEFAULT_DB_PATH))).expanduser()),
        "--source-mode",
        str(form.get("benchmark_source_mode", "pykrx_index") or "pykrx_index"),
        "--source-csv",
        str(Path(form.get("benchmark_source_csv", str(_DEFAULT_KOSPI200_SOURCE_CSV_PATH))).expanduser()),
        "--export-csv",
        str(Path(form.get("benchmark_export_csv", str(_DEFAULT_KOSPI200_EXPORT_CSV_PATH))).expanduser()),
        "--constituent-count",
        str(form.get("benchmark_constituent_count", "200") or "200"),
    ]
    benchmark_as_of_date = str(form.get("benchmark_as_of_date", "") or "").strip()
    if benchmark_as_of_date:
        command.extend(["--as-of-date", benchmark_as_of_date])
    return command


def _nav_html(active_path: str) -> str:
    refresh_href = "/refresh"
    benchmark_href = "/benchmark"
    refresh_class = "badge" if active_path == refresh_href else ""
    benchmark_class = "badge" if active_path == benchmark_href else ""
    return (
        f'<div class="actions">'
        f'<a class="{refresh_class}" href="{refresh_href}">데이터 갱신</a>'
        f'<a class="{benchmark_class}" href="{benchmark_href}">KOSPI200 관리</a>'
        f"</div>"
    )


def _base_css() -> str:
    return """
    :root {
      --bg: #f4f7f2;
      --panel: #ffffff;
      --line: #d8e1d3;
      --text: #17212b;
      --muted: #5e6b61;
      --brand: #1f6a3b;
      --brand-soft: #e8f4ec;
      --warn: #a35b00;
      --danger: #b42318;
      --shadow: 0 10px 28px rgba(15, 23, 42, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        radial-gradient(circle at top left, rgba(31, 106, 59, 0.08), transparent 28%),
        linear-gradient(180deg, #f9fbf8 0%, var(--bg) 100%);
      color: var(--text);
      font-family: "Segoe UI", "Noto Sans KR", sans-serif;
    }
    .wrap { max-width: 1360px; margin: 0 auto; padding: 18px; }
    .hero, .card, .pane {
      background: rgba(255, 255, 255, 0.92);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: var(--shadow);
    }
    .hero { padding: 18px 20px; margin-bottom: 14px; }
    .hero h1 { margin: 0 0 8px; font-size: 28px; }
    .hero p { margin: 0; color: var(--muted); line-height: 1.55; }
    .card { padding: 16px; margin-bottom: 14px; }
    .grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(220px, 1fr));
      gap: 12px;
    }
    .field label { display: block; font-size: 12px; font-weight: 700; color: var(--muted); margin-bottom: 6px; }
    .field input[type="text"],
    .field input[type="date"],
    .field input[type="number"],
    .field input[type="password"] {
      width: 100%;
      min-height: 40px;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 0 12px;
      font-size: 14px;
      background: #fff;
      color: var(--text);
    }
    .checks { display: flex; flex-wrap: wrap; gap: 10px 14px; padding-top: 10px; }
    .checks label { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; }
    .actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }
    button {
      min-height: 42px;
      padding: 0 16px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--text);
      font-weight: 700;
      cursor: pointer;
    }
    .actions a {
      min-height: 42px;
      padding: 10px 16px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--text);
      font-weight: 700;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
    }
    button.primary { background: var(--brand); border-color: var(--brand); color: #fff; }
    button.ghost { background: var(--brand-soft); border-color: #bfd6c7; color: var(--brand); }
    .meta { margin-top: 10px; font-size: 12px; color: var(--muted); line-height: 1.6; }
    .status-line { color: var(--muted); font-size: 12px; margin-top: 8px; }
    .status-line strong { color: var(--text); }
    .split-grid { display: grid; grid-template-columns: 1.4fr 1fr; gap: 12px; }
    .pane { padding: 14px; min-height: 520px; }
    .pane h3 { margin: 0 0 10px; }
    .refresh-stage-grid { display: grid; gap: 12px; margin-top: 14px; }
    .refresh-stage-card {
      background: rgba(255, 255, 255, 0.94);
      border: 1px solid var(--line);
      border-left: 5px solid var(--brand);
      border-radius: 18px;
      padding: 14px;
      box-shadow: var(--shadow);
    }
    .refresh-stage-head {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(280px, 0.9fr);
      gap: 12px;
      align-items: start;
      margin-bottom: 10px;
    }
    .refresh-stage-head h3 { margin: 0 0 6px; }
    .refresh-stage-head p { margin: 0; color: var(--muted); font-size: 13px; line-height: 1.5; }
    .refresh-stage-meta {
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #f7fbf8;
      padding: 10px 12px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }
    .refresh-stage-meta strong { color: var(--text); }
    .refresh-stage-latest {
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #f2f8f4;
      padding: 10px 12px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
      margin-top: 8px;
    }
    .refresh-stage-latest strong { display: block; margin-bottom: 4px; color: var(--text); }
    .refresh-stage-latest div + div { margin-top: 2px; }
    .refresh-stage-split {
      display: grid;
      grid-template-columns: minmax(0, 2fr) minmax(260px, 1fr);
      gap: 12px;
    }
    .refresh-stage-pane {
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fff;
      padding: 12px;
    }
    .refresh-stage-pane-title {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 8px;
    }
    .refresh-stage-pane-title h4 {
      margin: 0;
      font-size: 14px;
    }
    .refresh-stage-pane-title span {
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }
    .line-list {
      height: 460px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fff;
      padding: 8px;
    }
    .line {
      font-family: Consolas, "Courier New", monospace;
      font-size: 12px;
      line-height: 1.45;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      border-bottom: 1px solid #eef2f0;
      padding: 4px 2px;
    }
    .line:last-child { border-bottom: 0; }
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 3px 8px;
      border-radius: 999px;
      background: var(--brand-soft);
      color: var(--brand);
      font-size: 12px;
      font-weight: 700;
    }
    .warn { color: var(--warn); }
    .danger { color: var(--danger); }
    @media (max-width: 1120px) {
      .grid { grid-template-columns: 1fr 1fr; }
      .split-grid { grid-template-columns: 1fr; }
      .refresh-stage-head { grid-template-columns: 1fr; }
      .refresh-stage-split { grid-template-columns: 1fr; }
    }
    @media (max-width: 760px) {
      .grid { grid-template-columns: 1fr; }
    }
    """


def _html_refresh_page(*, form: dict[str, str], saved_key_masked: str, status_note: str | None = None) -> str:
    saved_note = (
        f'저장된 DART 키: <span class="badge">{html.escape(saved_key_masked)}</span>'
        if saved_key_masked
        else '<span class="warn">저장된 DART 키가 없습니다.</span>'
    )
    status_note_html = f'<div class="status-line">{html.escape(status_note)}</div>' if status_note else ""
    stage_cards: list[str] = []
    for stage in _refresh_stage_defs():
        step_id = str(stage["step_id"])
        stage_cards.append(
            f"""
    <section class="refresh-stage-card" data-refresh-stage="{html.escape(step_id)}">
      <div class="refresh-stage-head">
        <div>
          <h3>{html.escape(str(stage["title"]))}</h3>
          <p>{html.escape(str(stage["description"]))}</p>
        </div>
        <div id="refresh-stage-meta-{html.escape(step_id)}" class="refresh-stage-meta">
          <strong>상태</strong>: 대기<br/>
          실행 ID: - / 시작: - / 종료: -
        </div>
        <div id="refresh-stage-latest-{html.escape(step_id)}" class="refresh-stage-latest">
          <strong>실행 전 최신 현황</strong>
          <div id="refresh-stage-latest-summary-{html.escape(step_id)}">최신 현황 확인 중...</div>
          <div id="refresh-stage-latest-items-{html.escape(step_id)}"></div>
        </div>
        <form method="post" action="/run_refresh" class="refresh-stage-run-form" data-refresh-stage-form="{html.escape(step_id)}">
          <input type="hidden" name="refresh_step" value="{html.escape(step_id)}" />
          <button class="primary" type="submit" id="refresh-stage-btn-{html.escape(step_id)}">{html.escape(str(stage["title"]))} 실행</button>
        </form>
      </div>
      <div class="refresh-stage-split">
        <div class="refresh-stage-pane">
          <div class="refresh-stage-pane-title"><h4>실행 로그</h4><span>Live</span></div>
          <div id="refresh-stage-log-{html.escape(step_id)}" class="line-list"><div class="line">아직 로그가 없습니다.</div></div>
        </div>
        <div class="refresh-stage-pane">
          <div class="refresh-stage-pane-title"><h4>갱신 결과</h4><span>Output</span></div>
          <div id="refresh-stage-updates-{html.escape(step_id)}" class="line-list"><div class="line">아직 갱신 결과가 없습니다.</div></div>
        </div>
      </div>
    </section>
            """
        )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{_APP_TITLE}</title>
  <style>{_base_css()}</style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>{_APP_TITLE}</h1>
      <p>미국 주식 파이프라인과 분리된 KRX 전용 데이터 갱신 페이지입니다. 종목 마스터, 가격, DART 분기재무를 개별 단계로 실행할 수 있고, DART API 키는 로컬 설정 파일에만 저장됩니다.</p>
      {_nav_html("/refresh")}
    </section>
    <form class="card" id="refresh-settings-form" method="post" action="/run_refresh" onsubmit="return false;">
      <div class="grid">
        <div class="field">
          <label>SQLite DB 경로</label>
          <input type="text" name="db_path" value="{html.escape(form.get("db_path", ""))}" />
        </div>
        <div class="field">
          <label>가격 시작일</label>
          <input type="date" name="start_date" value="{html.escape(form.get("start_date", ""))}" />
        </div>
        <div class="field">
          <label>DART 시작연도</label>
          <input type="number" name="start_year" value="{html.escape(form.get("start_year", ""))}" min="2000" max="2100" />
        </div>
        <div class="field">
          <label>DART 종료연도</label>
          <input type="number" name="end_year" value="{html.escape(form.get("end_year", ""))}" min="2000" max="2100" />
        </div>
        <div class="field">
          <label>요청 간 대기초</label>
          <input type="text" name="pause_seconds" value="{html.escape(form.get("pause_seconds", ""))}" />
        </div>
        <div class="field">
          <label>CA Bundle 경로</label>
          <input type="text" name="ca_bundle_path" value="{html.escape(form.get("ca_bundle_path", ""))}" />
        </div>
        <div class="field" style="grid-column: 1 / -1;">
          <label>DART API 키</label>
          <input type="password" name="dart_api_key" value="" placeholder="새 키를 입력하면 이번 실행에 사용합니다" autocomplete="new-password" />
        </div>
      </div>
      <div class="checks">
        <label><input type="checkbox" name="use_saved_api_key" {"checked" if form.get("use_saved_api_key", "") == "on" else ""} /> 저장된 DART 키 사용</label>
        <label><input type="checkbox" name="save_api_key" {"checked" if form.get("save_api_key", "") == "on" else ""} /> 이번에 입력한 키를 로컬에 저장</label>
        <label><input type="checkbox" name="insecure_ssl" {"checked" if form.get("insecure_ssl", "") == "on" else ""} /> Insecure SSL</label>
      </div>
      <div class="meta">{saved_note}</div>
      {status_note_html}
      <p id="refresh-meta" class="meta">상태: 대기 / 실행 ID: - / 시작: - / 종료: -</p>
    </form>
    <form class="card" method="post" action="/delete_saved_api_key">
      <div class="actions">
        <button class="ghost" type="submit">저장된 DART 키 삭제</button>
      </div>
      <div class="meta">배포판에서는 사용자가 여기서 직접 키를 입력하거나 로컬 저장 후 사용할 수 있습니다. 키는 subprocess 환경변수로만 전달합니다.</div>
    </form>
    <div class="refresh-stage-grid">
      {"".join(stage_cards)}
    </div>
  </div>
  <script>
    const metaEl = document.getElementById("refresh-meta");
    const settingsFormEl = document.getElementById("refresh-settings-form");
    const refreshStageLogCounts = {{}};
    const refreshStageUpdateKeys = {{}};

    function esc(value) {{
      return String(value ?? "-")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }}

    function renderItems(el, items, emptyText) {{
      if (!el) return;
      if (!items || items.length === 0) {{
        el.innerHTML = "<div class='line'>" + esc(emptyText) + "</div>";
        return;
      }}
      el.innerHTML = items.map((item) => {{
        const line = (item.dataset || "-")
          + " | latest=" + (item.latest_date || "-")
          + " | rows=" + (item.rows ?? "-")
          + " | source=" + (item.source || "-");
        return "<div class='line'>" + esc(line) + "</div>";
      }}).join("");
    }}

    function renderLatest(summaryEl, itemsEl, summary, items) {{
      if (summaryEl) {{
        summaryEl.textContent = summary || "현재 DB 상태를 확인할 수 없습니다.";
      }}
      if (!itemsEl) return;
      if (!items || items.length === 0) {{
        itemsEl.innerHTML = "";
        return;
      }}
      itemsEl.innerHTML = items.map((item) => {{
        const line = (item.dataset || "-")
          + " | latest=" + (item.latest_date || "-")
          + " | rows=" + (item.rows ?? "-")
          + " | source=" + (item.source || "-");
        return "<div>" + esc(line) + "</div>";
      }}).join("");
    }}

    async function pollStatus() {{
      try {{
        const res = await fetch("/refresh_status", {{ cache: "no-store" }});
        if (!res.ok) {{
          metaEl.textContent = "상태: 오류 (상태 조회 실패)";
          return;
        }}
        const data = await res.json();
        const isRunning = Boolean(data.running);
        metaEl.textContent = "상태: " + (data.status || "대기")
          + " / 실행 ID: " + (data.run_id || "-")
          + " / 시작: " + (data.started_at || "-")
          + " / 종료: " + (data.finished_at || "-");

        const stages = Array.isArray(data.stages) ? data.stages : [];
        stages.forEach((stage) => {{
          const stepId = String(stage.step_id || "");
          const metaStageEl = document.getElementById("refresh-stage-meta-" + stepId);
          const btnEl = document.getElementById("refresh-stage-btn-" + stepId);
          const latestSummaryEl = document.getElementById("refresh-stage-latest-summary-" + stepId);
          const latestItemsEl = document.getElementById("refresh-stage-latest-items-" + stepId);
          const logEl = document.getElementById("refresh-stage-log-" + stepId);
          const updatesEl = document.getElementById("refresh-stage-updates-" + stepId);
          renderLatest(latestSummaryEl, latestItemsEl, stage.latest_summary || "", stage.latest_items || []);
          if (metaStageEl) {{
            metaStageEl.innerHTML =
              "<strong>상태</strong>: " + esc(stage.status || "대기")
              + "<br/>실행 ID: " + esc(data.run_id || "-")
              + " / 시작: " + esc(stage.started_at || "-")
              + " / 종료: " + esc(stage.finished_at || "-");
          }}
          if (btnEl) {{
            const stageRunning = isRunning && String(stage.status || "") === "running";
            btnEl.disabled = isRunning;
            btnEl.style.opacity = isRunning ? "0.6" : "1";
            btnEl.textContent = stageRunning ? "실행 중..." : (String(stage.title || stepId) + " 실행");
          }}
          const logCount = Number(stage.log_count || 0);
          if (refreshStageLogCounts[stepId] !== logCount) {{
            refreshStageLogCounts[stepId] = logCount;
            const logs = Array.isArray(stage.logs) ? stage.logs : [];
            logEl.innerHTML = logs.length
              ? logs.map((line) => "<div class='line'>" + esc(line) + "</div>").join("")
              : "<div class='line'>아직 로그가 없습니다.</div>";
            logEl.scrollTop = logEl.scrollHeight;
          }}
          const updateKey = JSON.stringify(stage.updated_items || []);
          if (refreshStageUpdateKeys[stepId] !== updateKey) {{
            refreshStageUpdateKeys[stepId] = updateKey;
            renderItems(updatesEl, stage.updated_items || [], "아직 갱신 결과가 없습니다.");
          }}
        }});
      }} catch (err) {{
        metaEl.textContent = "상태: 오류 (" + String(err) + ")";
      }}
    }}

    document.querySelectorAll(".refresh-stage-run-form").forEach((formEl) => {{
      formEl.addEventListener("submit", async (event) => {{
        event.preventDefault();
        try {{
          const body = new URLSearchParams(new FormData(settingsFormEl));
          const stageData = new FormData(formEl);
          for (const [key, value] of stageData.entries()) {{
            body.set(key, value);
          }}
          const res = await fetch(formEl.action, {{
            method: "POST",
            headers: {{
              "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
              "Accept": "application/json",
              "X-Requested-With": "fetch",
            }},
            body,
          }});
          const data = await res.json().catch(() => ({{ ok: false, error: "응답을 해석할 수 없습니다." }}));
          if (!res.ok || !data.ok) {{
            window.alert(data.error || "작업 시작에 실패했습니다.");
            return;
          }}
          await pollStatus();
        }} catch (err) {{
          window.alert("작업 요청 중 오류가 발생했습니다.");
        }}
      }});
    }});

    pollStatus();
    setInterval(pollStatus, 2200);
  </script>
</body>
</html>
"""


def _html_benchmark_page(
    *,
    form: dict[str, str],
    status_note: str | None = None,
) -> str:
    status_note_html = f'<div class="status-line">{html.escape(status_note)}</div>' if status_note else ""
    manual_csv_hint = (
        "기본값은 pykrx의 코스피200 인덱스 구성종목 자동조회입니다. "
        "수동 CSV는 Symbol, MemberOrder, Notes 컬럼을 지원하고, proxy는 비상 대체용입니다."
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>KOSPI200 Benchmark Manager</title>
  <style>{_base_css()}</style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>KOSPI200 Benchmark Manager</h1>
      <p>KRX 프로젝트 안에서 벤치마크 포트폴리오로 쓸 KOSPI200 스냅샷을 구성하고 관리합니다. 기본은 pykrx의 코스피200 인덱스 구성종목 자동 동기화이고, 수동 CSV와 시총 proxy는 보조 수단으로 남겨둡니다.</p>
      {_nav_html("/benchmark")}
    </section>
    <form class="card" method="post" action="/run_benchmark">
      <div class="grid">
        <div class="field">
          <label>SQLite DB 경로</label>
          <input type="text" name="benchmark_db_path" value="{html.escape(form.get("benchmark_db_path", ""))}" />
        </div>
        <div class="field">
          <label>기준일</label>
          <input type="date" name="benchmark_as_of_date" value="{html.escape(form.get("benchmark_as_of_date", ""))}" />
        </div>
        <div class="field">
          <label>구성 방식</label>
          <select name="benchmark_source_mode" style="width: 100%; min-height: 40px; border: 1px solid var(--line); border-radius: 12px; padding: 0 12px; font-size: 14px; background: #fff; color: var(--text);">
            <option value="pykrx_index" {"selected" if form.get("benchmark_source_mode", "pykrx_index") == "pykrx_index" else ""}>pykrx 코스피200 자동동기화</option>
            <option value="top200_proxy" {"selected" if form.get("benchmark_source_mode", "") == "top200_proxy" else ""}>시총 상위 200 proxy</option>
            <option value="manual_csv" {"selected" if form.get("benchmark_source_mode", "") == "manual_csv" else ""}>수동 CSV</option>
          </select>
        </div>
        <div class="field">
          <label>수동 CSV 경로</label>
          <input type="text" name="benchmark_source_csv" value="{html.escape(form.get("benchmark_source_csv", ""))}" />
        </div>
        <div class="field">
          <label>내보내기 CSV 경로</label>
          <input type="text" name="benchmark_export_csv" value="{html.escape(form.get("benchmark_export_csv", ""))}" />
        </div>
        <div class="field">
          <label>Proxy 종목 수</label>
          <input type="number" name="benchmark_constituent_count" value="{html.escape(form.get("benchmark_constituent_count", "200"))}" min="1" max="400" />
        </div>
      </div>
      <div class="meta">{html.escape(manual_csv_hint)}</div>
      <div class="actions">
        <button class="primary" type="submit">KOSPI200 스냅샷 생성</button>
      </div>
      {status_note_html}
      <p id="benchmark-meta" class="meta">상태: 대기 / 실행 ID: - / 시작: - / 종료: -</p>
    </form>
    <div class="split-grid">
      <div class="pane">
        <h3>작업 로그</h3>
        <div id="benchmark-log" class="line-list"><div class="line">아직 로그가 없습니다.</div></div>
      </div>
      <div class="pane">
        <h3>현재 KOSPI200 스냅샷</h3>
        <div id="benchmark-snapshot" class="line-list"><div class="line">아직 저장된 스냅샷이 없습니다.</div></div>
      </div>
    </div>
  </div>
  <script>
    const metaEl = document.getElementById("benchmark-meta");
    const logEl = document.getElementById("benchmark-log");
    const snapshotEl = document.getElementById("benchmark-snapshot");
    let lastLogCount = -1;
    let lastSnapshotKey = "";

    function esc(value) {{
      return String(value ?? "-")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }}

    async function pollStatus() {{
      try {{
        const res = await fetch("/benchmark_status", {{ cache: "no-store" }});
        if (!res.ok) {{
          metaEl.textContent = "상태: 오류 (상태 조회 실패)";
          return;
        }}
        const data = await res.json();
        metaEl.textContent = "상태: " + (data.status || "대기")
          + " / 실행 ID: " + (data.run_id || "-")
          + " / 시작: " + (data.started_at || "-")
          + " / 종료: " + (data.finished_at || "-");

        const logs = Array.isArray(data.logs) ? data.logs : [];
        const logCount = Number(data.log_count || 0);
        if (logCount !== lastLogCount) {{
          lastLogCount = logCount;
          logEl.innerHTML = logs.length
            ? logs.map((line) => "<div class='line'>" + esc(line) + "</div>").join("")
            : "<div class='line'>아직 로그가 없습니다.</div>";
          logEl.scrollTop = logEl.scrollHeight;
        }}

        const items = Array.isArray(data.snapshot_items) ? data.snapshot_items : [];
        const snapshotKey = JSON.stringify(items);
        if (snapshotKey !== lastSnapshotKey) {{
          lastSnapshotKey = snapshotKey;
          snapshotEl.innerHTML = items.length
            ? items.map((item) => {{
                const weightPct = (Number(item.benchmark_weight || 0) * 100).toFixed(4);
                const line = (item.member_order || "-")
                  + " | " + (item.symbol || "-")
                  + " | " + (item.name_kr || "-")
                  + " | weight=" + weightPct + "%"
                  + " | sector=" + (item.sector || "-");
                return "<div class='line'>" + esc(line) + "</div>";
              }}).join("")
            : "<div class='line'>아직 저장된 스냅샷이 없습니다.</div>";
        }}
      }} catch (err) {{
        metaEl.textContent = "상태: 오류 (" + String(err) + ")";
      }}
    }}

    pollStatus();
    setInterval(pollStatus, 2200);
  </script>
</body>
</html>
"""


def launch_web_gui(host: str = "localhost", port: int = 8517, open_browser: bool = False) -> None:
    root_dir = _project_root_dir()

    class Handler(BaseHTTPRequestHandler):
        state_form: dict[str, str] = _default_form(root_dir)
        state_benchmark_form: dict[str, str] = _default_benchmark_form(root_dir)
        state_saved_key_masked: str = _mask_secret(_read_saved_api_key(root_dir))
        state_refresh_run_id: int = 0
        state_refresh_running: bool = False
        state_refresh_status: str = "idle"
        state_refresh_started_at: str | None = None
        state_refresh_finished_at: str | None = None
        state_refresh_error: str | None = None
        state_refresh_logs: list[str] = []
        state_refresh_live_items: list[dict[str, object]] = []
        state_refresh_latest_items: list[dict[str, object]] = []
        state_refresh_latest_loaded_at: float = 0.0
        state_refresh_stage_states: dict[str, dict[str, object]] = _empty_refresh_stage_states()
        state_refresh_history: list[dict[str, object]] = []
        state_benchmark_run_id: int = 0
        state_benchmark_running: bool = False
        state_benchmark_status: str = "idle"
        state_benchmark_started_at: str | None = None
        state_benchmark_finished_at: str | None = None
        state_benchmark_error: str | None = None
        state_benchmark_logs: list[str] = []
        state_benchmark_snapshot_items: list[dict[str, object]] = _collect_benchmark_snapshot_items(
            Path(str(_default_benchmark_form(root_dir).get("benchmark_db_path", _DEFAULT_DB_PATH))).expanduser()
        )
        refresh_lock = threading.Lock()
        benchmark_lock = threading.Lock()

        @classmethod
        def _append_refresh_log(cls, message: str) -> None:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = f"[{stamp}] {message}"
            with cls.refresh_lock:
                cls.state_refresh_logs.append(line)
                if len(cls.state_refresh_logs) > 800:
                    cls.state_refresh_logs = cls.state_refresh_logs[-800:]

        @classmethod
        def _append_refresh_stage_log(cls, step_id: str, message: str) -> None:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = f"[{stamp}] {message}"
            with cls.refresh_lock:
                stage = cls.state_refresh_stage_states.get(step_id)
                if stage is None:
                    return
                logs = list(stage.get("logs", []))
                logs.append(line)
                if len(logs) > 800:
                    logs = logs[-800:]
                stage["logs"] = logs
                stage["log_count"] = len(logs)

        @classmethod
        def _set_refresh_stage_status(
            cls,
            step_id: str,
            *,
            status: str,
            started_at: str | None = None,
            finished_at: str | None = None,
            updated_items: list[dict[str, object]] | None = None,
            selected: bool | None = None,
        ) -> None:
            with cls.refresh_lock:
                stage = cls.state_refresh_stage_states.get(step_id)
                if stage is None:
                    return
                stage["status"] = status
                if started_at is not None:
                    stage["started_at"] = started_at
                if finished_at is not None:
                    stage["finished_at"] = finished_at
                if updated_items is not None:
                    stage["updated_items"] = [dict(item) for item in updated_items]
                if selected is not None:
                    stage["selected"] = bool(selected)

        @classmethod
        def _refresh_status_payload(cls) -> dict[str, object]:
            current_items = cls._refresh_latest_items()
            with cls.refresh_lock:
                return {
                    "status": cls.state_refresh_status,
                    "run_id": cls.state_refresh_run_id,
                    "running": cls.state_refresh_running,
                    "started_at": cls.state_refresh_started_at,
                    "finished_at": cls.state_refresh_finished_at,
                    "error": cls.state_refresh_error,
                    "log_count": len(cls.state_refresh_logs),
                    "logs": list(cls.state_refresh_logs[-260:]),
                    "updated_items": [dict(item) for item in cls.state_refresh_live_items],
                    "stages": [
                        {
                            "step_id": step_id,
                            "label": str(stage.get("label") or ""),
                            "title": str(stage.get("title") or ""),
                            "status": str(stage.get("status") or "idle"),
                            "selected": bool(stage.get("selected")),
                            "started_at": stage.get("started_at"),
                            "finished_at": stage.get("finished_at"),
                            "log_count": int(stage.get("log_count") or 0),
                            "logs": list(stage.get("logs", [])[-260:]),
                            "updated_items": [dict(item) for item in stage.get("updated_items", [])],
                            "latest_items": _filter_refresh_items_for_step(current_items, step_id),
                            "latest_summary": _summarize_refresh_items(_filter_refresh_items_for_step(current_items, step_id)),
                        }
                        for step_id, stage in cls.state_refresh_stage_states.items()
                    ],
                }

        @classmethod
        def _refresh_latest_items(cls) -> list[dict[str, object]]:
            now = time.monotonic()
            with cls.refresh_lock:
                cached_items = [dict(item) for item in cls.state_refresh_latest_items]
                loaded_at = float(cls.state_refresh_latest_loaded_at or 0.0)
                form = dict(cls.state_form)
            if cached_items and now - loaded_at < 30.0:
                return cached_items

            db_path = Path(str(form.get("db_path", str(_DEFAULT_DB_PATH)))).expanduser()
            items = _collect_refresh_items(root_dir, db_path)
            with cls.refresh_lock:
                cls.state_refresh_latest_items = [dict(item) for item in items]
                cls.state_refresh_latest_loaded_at = time.monotonic()
            return [dict(item) for item in items]

        @classmethod
        def _persist_settings(cls, form: dict[str, str], entered_api_key: str | None) -> None:
            config = _load_gui_config(root_dir)
            config.update(
                {
                    "db_path": form.get("db_path", str(_DEFAULT_DB_PATH)),
                    "start_date": form.get("start_date", "2019-12-31"),
                    "start_year": form.get("start_year", "2019"),
                    "end_year": form.get("end_year", str(datetime.now().year)),
                    "pause_seconds": form.get("pause_seconds", "0.0"),
                    "ca_bundle_path": form.get("ca_bundle_path", ""),
                    "insecure_ssl": "on" if form.get("insecure_ssl", "") == "on" else "",
                    "run_components": "on" if form.get("run_components", "") == "on" else "",
                    "run_prices": "on" if form.get("run_prices", "") == "on" else "",
                    "run_fundamentals": "on" if form.get("run_fundamentals", "") == "on" else "",
                    "run_news": "on" if form.get("run_news", "") == "on" else "",
                }
            )
            if form.get("save_api_key", "") == "on" and entered_api_key:
                config["dart_api_key"] = entered_api_key
            _save_gui_config(root_dir, config)
            cls.state_saved_key_masked = _mask_secret(str(config.get("dart_api_key", "")))

        @classmethod
        def _append_benchmark_log(cls, message: str) -> None:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = f"[{stamp}] {message}"
            with cls.benchmark_lock:
                cls.state_benchmark_logs.append(line)
                if len(cls.state_benchmark_logs) > 800:
                    cls.state_benchmark_logs = cls.state_benchmark_logs[-800:]

        @classmethod
        def _benchmark_status_payload(cls) -> dict[str, object]:
            with cls.benchmark_lock:
                return {
                    "status": cls.state_benchmark_status,
                    "run_id": cls.state_benchmark_run_id,
                    "running": cls.state_benchmark_running,
                    "started_at": cls.state_benchmark_started_at,
                    "finished_at": cls.state_benchmark_finished_at,
                    "error": cls.state_benchmark_error,
                    "log_count": len(cls.state_benchmark_logs),
                    "logs": list(cls.state_benchmark_logs[-260:]),
                    "snapshot_items": [dict(item) for item in cls.state_benchmark_snapshot_items],
                }

        @classmethod
        def _persist_benchmark_settings(cls, form: dict[str, str]) -> None:
            config = _load_gui_config(root_dir)
            config.update(
                {
                    "benchmark_db_path": form.get("benchmark_db_path", str(_DEFAULT_DB_PATH)),
                    "benchmark_as_of_date": form.get("benchmark_as_of_date", ""),
                    "benchmark_source_mode": form.get("benchmark_source_mode", "pykrx_index"),
                    "benchmark_source_csv": form.get("benchmark_source_csv", str(_DEFAULT_KOSPI200_SOURCE_CSV_PATH)),
                    "benchmark_export_csv": form.get("benchmark_export_csv", str(_DEFAULT_KOSPI200_EXPORT_CSV_PATH)),
                    "benchmark_constituent_count": form.get("benchmark_constituent_count", "200"),
                }
            )
            _save_gui_config(root_dir, config)

        @classmethod
        def _start_refresh_job(cls, form: dict[str, str]) -> tuple[bool, str]:
            with cls.refresh_lock:
                if cls.state_refresh_running:
                    return False, "A refresh job is already running."
                cls.state_refresh_run_id += 1
                run_id = cls.state_refresh_run_id
                cls.state_refresh_running = True
                cls.state_refresh_status = "running"
                cls.state_refresh_started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cls.state_refresh_finished_at = None
                cls.state_refresh_error = None
                cls.state_form = _sanitize_form_for_state(form)
                selected_by_step = {
                    "components": form.get("run_components", "") == "on",
                    "prices": form.get("run_prices", "") == "on",
                    "fundamentals": form.get("run_fundamentals", "") == "on",
                    "news": form.get("run_news", "") == "on",
                }
                for step_id, selected in selected_by_step.items():
                    stage = cls.state_refresh_stage_states.get(step_id)
                    if stage is None:
                        continue
                    stage["selected"] = selected
                    if selected:
                        stage["status"] = "queued"
                        stage["started_at"] = None
                        stage["finished_at"] = None

            thread = threading.Thread(target=cls._run_refresh_job, args=(run_id, dict(form)), daemon=True)
            thread.start()
            cls._append_refresh_log(f"Run {run_id} started.")
            return True, f"Run {run_id} started."

        @classmethod
        def _start_benchmark_job(cls, form: dict[str, str]) -> tuple[bool, str]:
            with cls.benchmark_lock:
                if cls.state_benchmark_running:
                    return False, "A benchmark job is already running."
                cls.state_benchmark_run_id += 1
                run_id = cls.state_benchmark_run_id
                cls.state_benchmark_running = True
                cls.state_benchmark_status = "running"
                cls.state_benchmark_started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cls.state_benchmark_finished_at = None
                cls.state_benchmark_error = None
                cls.state_benchmark_logs = []
                cls.state_benchmark_form = _sanitize_benchmark_form_for_state(form)

            thread = threading.Thread(target=cls._run_benchmark_job, args=(run_id, dict(form)), daemon=True)
            thread.start()
            cls._append_benchmark_log(f"Run {run_id} started.")
            return True, f"Run {run_id} started."

        @classmethod
        def _run_refresh_job(cls, run_id: int, form: dict[str, str]) -> None:
            status = "success"
            error_message: str | None = None
            db_path = Path(str(form.get("db_path", str(_DEFAULT_DB_PATH)))).expanduser()
            try:
                entered_api_key = str(form.get("dart_api_key", "")).strip()
                cls._persist_settings(form, entered_api_key or None)
                config = _load_gui_config(root_dir)
                saved_api_key = str(config.get("dart_api_key", "")).strip()
                api_key = entered_api_key or (saved_api_key if form.get("use_saved_api_key", "") == "on" else "")

                steps = _build_refresh_steps(root_dir=root_dir, form=form, api_key=api_key)
                if not steps:
                    raise ValueError("최소 한 개 이상의 실행 단계를 선택해 주세요.")

                for step_id, label, command, extra_env in steps:
                    stage_started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cls._set_refresh_stage_status(step_id, status="running", started_at=stage_started_at, selected=True)
                    cls._append_refresh_log(f"Executing {label}: {' '.join(command[:5])} ...")
                    cls._append_refresh_stage_log(step_id, f"Executing {label}: {' '.join(command[:5])} ...")
                    env = os.environ.copy()
                    env.update(extra_env)
                    proc = subprocess.Popen(
                        command,
                        cwd=str(root_dir),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                        env=env,
                    )
                    if proc.stdout is not None:
                        for line in proc.stdout:
                            stripped = line.rstrip("\r\n")
                            if stripped:
                                cls._append_refresh_log(stripped)
                                cls._append_refresh_stage_log(step_id, stripped)
                    exit_code = int(proc.wait())
                    if exit_code != 0:
                        cls._set_refresh_stage_status(
                            step_id,
                            status="error",
                            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        )
                        raise RuntimeError(f"{label} exited with code {exit_code}")
                    cls.state_refresh_live_items = _collect_refresh_items(root_dir, db_path)
                    cls.state_refresh_latest_items = [dict(item) for item in cls.state_refresh_live_items]
                    cls.state_refresh_latest_loaded_at = time.monotonic()
                    stage_items = _filter_refresh_items_for_step(cls.state_refresh_live_items, step_id)
                    cls._set_refresh_stage_status(
                        step_id,
                        status="success",
                        finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        updated_items=stage_items,
                    )
                    cls._append_refresh_log(f"{label} finished successfully.")
                    cls._append_refresh_stage_log(step_id, f"{label} finished successfully.")
            except Exception as exc:
                status = "error"
                if isinstance(exc, ValueError):
                    error_message = str(exc)
                else:
                    error_message = f"{type(exc).__name__}: {exc}"
                cls._append_refresh_log(f"[error] {error_message}")
                cls._append_refresh_log(traceback.format_exc().strip())

            finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            history_row = {
                "run_id": run_id,
                "status": status,
                "started_at": cls.state_refresh_started_at,
                "finished_at": finished_at,
                "error_message": error_message,
                "updated_items": [dict(item) for item in cls.state_refresh_live_items],
            }

            with cls.refresh_lock:
                cls.state_refresh_running = False
                cls.state_refresh_status = status
                cls.state_refresh_finished_at = finished_at
                cls.state_refresh_error = error_message
                cls.state_saved_key_masked = _mask_secret(_read_saved_api_key(root_dir))
                for step_id, stage in cls.state_refresh_stage_states.items():
                    if bool(stage.get("selected")) and str(stage.get("status")) in {"idle", "queued", "running"}:
                        stage["status"] = "cancelled" if status == "error" else "idle"
                        if stage.get("finished_at") is None:
                            stage["finished_at"] = finished_at
                cls.state_refresh_history.insert(0, history_row)
                if len(cls.state_refresh_history) > 200:
                    cls.state_refresh_history = cls.state_refresh_history[:200]

            cls._append_refresh_log(f"Run {run_id} finished with status={status}.")

        @classmethod
        def _run_benchmark_job(cls, run_id: int, form: dict[str, str]) -> None:
            status = "success"
            error_message: str | None = None
            db_path = Path(str(form.get("benchmark_db_path", str(_DEFAULT_DB_PATH)))).expanduser()
            try:
                cls._persist_benchmark_settings(form)
                command = _build_benchmark_command(root_dir=root_dir, form=form)
                cls._append_benchmark_log(f"Executing KOSPI200 benchmark sync: {' '.join(command[:6])} ...")
                proc = subprocess.Popen(
                    command,
                    cwd=str(root_dir),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=os.environ.copy(),
                )
                if proc.stdout is not None:
                    for line in proc.stdout:
                        stripped = line.rstrip("\r\n")
                        if stripped:
                            cls._append_benchmark_log(stripped)
                exit_code = int(proc.wait())
                if exit_code != 0:
                    raise RuntimeError(f"KOSPI200 benchmark sync exited with code {exit_code}")
                cls.state_benchmark_snapshot_items = _collect_benchmark_snapshot_items(db_path)
                cls._append_benchmark_log("KOSPI200 benchmark sync finished successfully.")
            except Exception as exc:
                status = "error"
                error_message = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}"
                cls._append_benchmark_log(f"[error] {error_message}")
                cls._append_benchmark_log(traceback.format_exc().strip())

            finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with cls.benchmark_lock:
                cls.state_benchmark_running = False
                cls.state_benchmark_status = status
                cls.state_benchmark_finished_at = finished_at
                cls.state_benchmark_error = error_message
                cls.state_benchmark_snapshot_items = _collect_benchmark_snapshot_items(db_path)

            cls._append_benchmark_log(f"Run {run_id} finished with status={status}.")

        def _read_form(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            parsed = parse_qs(raw, keep_blank_values=True)
            return {key: values[-1] if values else "" for key, values in parsed.items()}

        def _wants_json(self) -> bool:
            accept = str(self.headers.get("Accept", ""))
            requested_with = str(self.headers.get("X-Requested-With", ""))
            return "application/json" in accept or requested_with.lower() == "fetch"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/refresh"}:
                self._send_html(
                    _html_refresh_page(
                        form=self.__class__.state_form,
                        saved_key_masked=self.__class__.state_saved_key_masked,
                    )
                )
                return
            if parsed.path == "/benchmark":
                self._send_html(
                    _html_benchmark_page(
                        form=self.__class__.state_benchmark_form,
                    )
                )
                return
            if parsed.path == "/refresh_status":
                self._send_json(self.__class__._refresh_status_payload())
                return
            if parsed.path == "/benchmark_status":
                self._send_json(self.__class__._benchmark_status_payload())
                return
            self.send_error(404)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/run_refresh":
                self._handle_refresh_run()
                return
            if parsed.path == "/run_benchmark":
                self._handle_benchmark_run()
                return
            if parsed.path == "/delete_saved_api_key":
                self._handle_delete_saved_api_key()
                return
            self.send_error(404)

        def _handle_refresh_run(self) -> None:
            form = self._read_form()
            for key in ("run_components", "run_prices", "run_fundamentals", "run_news", "save_api_key", "use_saved_api_key", "insecure_ssl"):
                if key not in form:
                    form[key] = ""
            refresh_step = str(form.get("refresh_step", "")).strip().lower()
            if refresh_step:
                step_to_flag = {
                    "components": "run_components",
                    "prices": "run_prices",
                    "fundamentals": "run_fundamentals",
                    "news": "run_news",
                }
                for flag in ("run_components", "run_prices", "run_fundamentals", "run_news"):
                    form[flag] = ""
                if refresh_step not in step_to_flag:
                    message = "알 수 없는 갱신 단계입니다."
                    if self._wants_json():
                        self._send_json({"ok": False, "error": message}, status=400)
                        return
                    started = False
                else:
                    form[step_to_flag[refresh_step]] = "on"
                    started, message = self.__class__._start_refresh_job(form)
            else:
                started, message = self.__class__._start_refresh_job(form)
            if not started:
                self.__class__._append_refresh_log(message)
            if self._wants_json():
                self._send_json({"ok": bool(started), "message": message, "error": "" if started else message}, status=200 if started else 409)
                return
            self._send_html(
                _html_refresh_page(
                    form=self.__class__.state_form,
                    saved_key_masked=self.__class__.state_saved_key_masked,
                    status_note=message,
                )
            )

        def _handle_delete_saved_api_key(self) -> None:
            _delete_saved_api_key(root_dir)
            self.__class__.state_saved_key_masked = ""
            self.__class__.state_form = _default_form(root_dir)
            self._send_html(
                _html_refresh_page(
                    form=self.__class__.state_form,
                    saved_key_masked="",
                    status_note="저장된 DART 키를 삭제했습니다.",
                )
            )

        def _handle_benchmark_run(self) -> None:
            form = self._read_form()
            started, message = self.__class__._start_benchmark_job(form)
            if not started:
                self.__class__._append_benchmark_log(message)
            self._send_html(
                _html_benchmark_page(
                    form=self.__class__.state_benchmark_form,
                    status_note=message,
                )
            )

        def _send_html(self, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, payload_obj: dict[str, object], status: int = 200) -> None:
            payload = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"{_APP_TITLE} listening on http://{host}:{port}/refresh")
    if open_browser:
        _schedule_browser_open(host=host, port=port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


run_web_gui = launch_web_gui


if __name__ == "__main__":
    launch_web_gui()
