from __future__ import annotations

import argparse
import io
import os
import re
from pathlib import Path
from typing import Callable

import pandas as pd

from pipeline_common.security import configure_ssl

try:
    import requests
except Exception:  # pragma: no cover - optional dependency
    requests = None

try:
    import urllib3
    from urllib3.exceptions import InsecureRequestWarning
except Exception:  # pragma: no cover - optional dependency
    urllib3 = None
    InsecureRequestWarning = None

try:
    import FinanceDataReader as fdr
except Exception:  # pragma: no cover - optional dependency
    fdr = None


WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
GITHUB_COMPONENTS_CSV_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
DATAHUB_COMPONENTS_CSV_URL = "https://datahub.io/core/s-and-p-500-companies/r/constituents.csv"


def _verify_value(ca_bundle: str | None, insecure_ssl: bool) -> bool | str:
    if insecure_ssl:
        return False
    if ca_bundle:
        return str(ca_bundle)

    env_ca = str(os.getenv("KEUMJ_CA_BUNDLE", "")).strip() or str(os.getenv("REQUESTS_CA_BUNDLE", "")).strip()
    if env_ca:
        return env_ca
    return True


def _http_headers() -> dict[str, str]:
    return {
        "User-Agent": "Keumj Components Builder/1.0 (+https://example.invalid)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/csv,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }


def _http_get(url: str, *, verify: bool | str):
    if requests is None:
        raise RuntimeError("requests is not available")
    session = requests.Session()
    resp = session.get(url, timeout=40, verify=verify, headers=_http_headers())
    resp.raise_for_status()
    return resp


def _normalize_symbol(value: object) -> str:
    txt = str(value or "").strip().upper()
    txt = txt.replace(" ", "")
    txt = re.sub(r"[^A-Z0-9._-]+", "", txt)
    return txt


def _finalize_components(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["Symbol", "Sector"])

    out = df.copy()
    out["Symbol"] = out["Symbol"].map(_normalize_symbol)
    out["Sector"] = out["Sector"].astype(str).str.strip()
    out = out[(out["Symbol"] != "") & (~out["Symbol"].isna())]
    out["Sector"] = out["Sector"].replace({"": "Unknown", "nan": "Unknown", "None": "Unknown"})
    out = out.drop_duplicates("Symbol").sort_values("Symbol").reset_index(drop=True)
    return out[["Symbol", "Sector"]]


def _from_fdr() -> tuple[pd.DataFrame, str]:
    if fdr is None:
        raise RuntimeError("FinanceDataReader is not available")

    frame = fdr.StockListing("S&P500")
    cols = {str(c).lower(): c for c in frame.columns}
    sym_col = cols.get("symbol")
    sec_col = cols.get("sector")
    if sym_col is None:
        raise RuntimeError("StockListing response has no Symbol column")

    if sec_col is None:
        out = frame[[sym_col]].copy()
        out["Sector"] = "Unknown"
        out.columns = ["Symbol", "Sector"]
    else:
        out = frame[[sym_col, sec_col]].copy()
        out.columns = ["Symbol", "Sector"]

    return _finalize_components(out), "fdr"


def _from_wikipedia(verify: bool | str) -> tuple[pd.DataFrame, str]:
    resp = _http_get(WIKI_URL, verify=verify)
    tables = pd.read_html(io.StringIO(resp.text))
    if not tables:
        raise RuntimeError("No HTML tables found on Wikipedia page")

    best: pd.DataFrame | None = None
    for table in tables:
        cols = {str(c).lower(): c for c in table.columns}
        if "symbol" in cols and ("gics sector" in cols or "sector" in cols):
            best = table
            break
    if best is None:
        raise RuntimeError("Could not find S&P500 components table on Wikipedia")

    cols = {str(c).lower(): c for c in best.columns}
    sym_col = cols["symbol"]
    sec_col = cols.get("gics sector") or cols.get("sector")

    out = best[[sym_col]].copy()
    out.columns = ["Symbol"]
    if sec_col is None:
        out["Sector"] = "Unknown"
    else:
        out["Sector"] = best[sec_col]

    return _finalize_components(out), "wikipedia"


def _from_remote_components_csv(url: str, *, source_name: str, verify: bool | str) -> tuple[pd.DataFrame, str]:
    resp = _http_get(url, verify=verify)
    frame = pd.read_csv(io.StringIO(resp.text))
    if frame.empty:
        raise RuntimeError(f"remote csv is empty: {url}")

    cols = {str(c).lower(): c for c in frame.columns}
    sym_col = cols.get("symbol")
    sec_col = cols.get("gics sector") or cols.get("sector")
    if sym_col is None:
        raise RuntimeError(f"remote csv has no Symbol column: {url}")

    out = frame[[sym_col]].copy()
    out.columns = ["Symbol"]
    if sec_col is None:
        out["Sector"] = "Unknown"
    else:
        out["Sector"] = frame[sec_col]

    return _finalize_components(out), source_name


def _from_symbols_csv(path: Path) -> tuple[pd.DataFrame, str]:
    if not path.exists() or not path.is_file():
        raise RuntimeError(f"symbols-csv not found: {path}")

    frame = pd.read_csv(path)
    if frame.empty:
        raise RuntimeError(f"symbols-csv is empty: {path}")

    cols = {str(c).lower(): c for c in frame.columns}
    sym_col = cols.get("symbol")
    sec_col = cols.get("sector")
    if sym_col is None:
        raise RuntimeError(f"symbols-csv has no Symbol column: {path}")

    out = frame[[sym_col]].copy()
    out.columns = ["Symbol"]
    if sec_col is None:
        out["Sector"] = "Unknown"
    else:
        out["Sector"] = frame[sec_col]

    return _finalize_components(out), f"csv:{path}"


def _from_prices_wide(path: Path) -> tuple[pd.DataFrame, str]:
    if not path.exists() or not path.is_file():
        raise RuntimeError(f"wide prices csv not found: {path}")

    frame = pd.read_csv(path, nrows=1)
    if frame.empty:
        raise RuntimeError(f"wide prices csv is empty: {path}")

    cols = [str(c) for c in frame.columns]
    data_cols = [c for c in cols if c.lower() not in {"date", "datetime"}]
    close_cols = [c.rsplit("_", 1)[0] for c in data_cols if c.rsplit("_", 1)[-1].lower() == "close"]
    sym_cols = close_cols or data_cols
    if not sym_cols:
        raise RuntimeError(f"No symbol columns found in prices csv: {path}")

    out = pd.DataFrame({"Symbol": sym_cols, "Sector": ["Unknown"] * len(sym_cols)})
    return _finalize_components(out), f"wide_prices:{path}"


def _from_shared_db_prices(path: Path) -> tuple[pd.DataFrame, str]:
    if not path.exists() or not path.is_dir():
        raise RuntimeError(f"shared-db directory not found: {path}")

    symbols: list[str] = []
    for csv in path.glob("*.csv"):
        symbols.append(csv.stem)

    if not symbols:
        raise RuntimeError(f"No symbol csv files found in shared-db directory: {path}")

    out = pd.DataFrame({"Symbol": symbols, "Sector": ["Unknown"] * len(symbols)})
    return _finalize_components(out), f"shared_db:{path}"


def build_sp500_components_csv(
    *,
    output_path: Path,
    min_count: int = 400,
    allow_small: bool = False,
    symbols_csv: Path | None = None,
    prices_csv: Path | None = None,
    shared_prices_dir: Path | None = None,
    insecure_ssl: bool = False,
    ca_bundle: str | None = None,
) -> tuple[pd.DataFrame, str]:
    configure_ssl(insecure_ssl=insecure_ssl, ca_bundle=ca_bundle)
    verify = _verify_value(ca_bundle=ca_bundle, insecure_ssl=insecure_ssl)
    if insecure_ssl and urllib3 is not None and InsecureRequestWarning is not None:
        urllib3.disable_warnings(InsecureRequestWarning)

    attempts: list[tuple[str, Callable[[], tuple[pd.DataFrame, str]]]] = []
    if symbols_csv is not None:
        attempts.append(("symbols_csv", lambda: _from_symbols_csv(symbols_csv)))
    attempts.extend(
        [
            (
                "github_constituents",
                lambda: _from_remote_components_csv(
                    GITHUB_COMPONENTS_CSV_URL,
                    source_name="github_constituents",
                    verify=verify,
                ),
            ),
            (
                "datahub_constituents",
                lambda: _from_remote_components_csv(
                    DATAHUB_COMPONENTS_CSV_URL,
                    source_name="datahub_constituents",
                    verify=verify,
                ),
            ),
            ("fdr", _from_fdr),
            ("wikipedia", lambda: _from_wikipedia(verify=verify)),
        ]
    )
    if prices_csv is not None:
        attempts.append(("prices_csv", lambda: _from_prices_wide(prices_csv)))
    if shared_prices_dir is not None:
        attempts.append(("shared_db", lambda: _from_shared_db_prices(shared_prices_dir)))

    errors: list[str] = []
    best_df: pd.DataFrame | None = None
    best_src: str | None = None

    for name, fn in attempts:
        try:
            df, src = fn()
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            continue

        if best_df is None or len(df) > len(best_df):
            best_df, best_src = df, src

        if len(df) >= min_count:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(output_path, index=False, encoding="utf-8")
            return df, src

    if best_df is not None and allow_small:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        best_df.to_csv(output_path, index=False, encoding="utf-8")
        return best_df, best_src or "best-effort"

    detail = "; ".join(errors[:4]) if errors else "no source available"
    found = 0 if best_df is None else len(best_df)
    raise ValueError(
        "Failed to build full S&P500 components CSV. "
        f"best_count={found}, required_min_count={min_count}. Details: {detail}. "
        "Tip: provide a valid CA bundle or use --insecure-ssl for temporary testing."
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build S&P500 components CSV (Symbol, Sector)")
    parser.add_argument("--output", default="data/sp500_components_full.csv", help="Output CSV path")
    parser.add_argument("--min-count", type=int, default=400, help="Minimum symbol count to accept as full universe")
    parser.add_argument("--allow-small", action="store_true", help="Allow writing best-effort small universe")
    parser.add_argument("--symbols-csv", default="", help="Optional source CSV with Symbol[,Sector]")
    parser.add_argument("--prices-csv", default="data/sp500_all_metrics_prices.csv", help="Fallback prices or metrics CSV path")
    parser.add_argument("--shared-prices-dir", default="data/sp500_shared_db/prices", help="Fallback shared-db prices dir")
    parser.add_argument("--ca-bundle", default="", help="CA bundle path")
    parser.add_argument("--insecure-ssl", action="store_true", help="Disable TLS verification for temporary testing")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    symbols_csv = Path(args.symbols_csv) if str(args.symbols_csv).strip() else None
    prices_csv = Path(args.prices_csv) if str(args.prices_csv).strip() else None
    shared_prices_dir = Path(args.shared_prices_dir) if str(args.shared_prices_dir).strip() else None

    df, src = build_sp500_components_csv(
        output_path=Path(args.output),
        min_count=int(args.min_count),
        allow_small=bool(args.allow_small),
        symbols_csv=symbols_csv,
        prices_csv=prices_csv,
        shared_prices_dir=shared_prices_dir,
        insecure_ssl=bool(args.insecure_ssl),
        ca_bundle=str(args.ca_bundle).strip() or None,
    )
    print(f"source={src}, rows={len(df)}, output={Path(args.output).resolve()}")


if __name__ == "__main__":
    main()
