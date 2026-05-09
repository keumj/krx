from __future__ import annotations

import argparse
import re
from pathlib import Path

import pandas as pd

from pipeline_common.security import configure_ssl

from .db import init_krx_project_db, sync_krx_security_snapshot

try:
    import FinanceDataReader as fdr
except Exception:  # pragma: no cover - optional dependency
    fdr = None


DEFAULT_COMPONENTS_CSV = Path("data/krx_components_full.csv")
DEFAULT_COMPONENTS_COMPACT_CSV = Path("data/krx_components.csv")
DEFAULT_COMPONENTS_UNKNOWN_CSV = Path("data/krx_components_unknown_sectors.csv")
DEFAULT_SECTOR_OVERRIDES_CSV = Path("data/krx_sector_overrides.csv")
DEFAULT_MARKETS = ("KOSPI", "KOSDAQ")
DEPT_LABELS = {
    "우량기업부",
    "중견기업부",
    "벤처기업부",
    "기술성장기업부",
    "관리종목(소속부없음)",
    "SPAC(소속부없음)",
    "투자주의환기종목(소속부없음)",
    "외국기업(소속부없음)",
}


def _normalize_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    if text.isdigit():
        return text.zfill(6)
    return text


def _normalize_text(value: object, *, fallback: str | None = None) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "null"}:
        return fallback
    return text


def _normalize_number(value: object) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text.lower() in {"nan", "none", "null", "n/a", "-"}:
        return None
    try:
        return float(text)
    except Exception:
        return None


def _normalize_date_text(value: object) -> str | None:
    text = _normalize_text(value)
    if text is None:
        return None
    try:
        return pd.Timestamp(text).normalize().strftime("%Y-%m-%d")
    except Exception:
        return text


def _column_name(frame: pd.DataFrame, *candidates: str) -> str | None:
    cols = {str(col).strip().lower(): col for col in frame.columns}
    for candidate in candidates:
        found = cols.get(candidate.strip().lower())
        if found is not None:
            return str(found)
    return None


def _looks_like_dept(value: object) -> bool:
    text = _normalize_text(value)
    if text is None:
        return False
    return text in DEPT_LABELS or text.endswith("기업부") or "소속부없음" in text


def _build_desc_lookup(market: str) -> dict[str, dict[str, object]]:
    desc_market = f"{market}-DESC"
    frame = fdr.StockListing(desc_market)
    if frame is None or frame.empty:
        return {}
    symbol_col = _column_name(frame, "code", "symbol", "종목코드")
    if symbol_col is None:
        return {}
    lookup: dict[str, dict[str, object]] = {}
    for item in frame.to_dict(orient="records"):
        symbol = _normalize_symbol(item.get(symbol_col))
        if not symbol:
            continue
        lookup[symbol] = item
    return lookup


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _load_sector_overrides(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists() or not path.is_file():
        return {}
    frame = pd.read_csv(path)
    if frame.empty:
        return {}
    cols = {str(col).strip().lower(): col for col in frame.columns}
    symbol_col = cols.get("symbol")
    sector_col = cols.get("sector")
    if symbol_col is None or sector_col is None:
        return {}
    overrides: dict[str, str] = {}
    for item in frame.to_dict(orient="records"):
        symbol = _normalize_symbol(item.get(symbol_col))
        sector = _normalize_text(item.get(sector_col))
        if symbol and sector:
            overrides[symbol] = sector
    return overrides


def _normalize_krx_sector(
    *,
    industry_detail: object,
    products_detail: object,
    company_name: object,
) -> str:
    industry_text = _normalize_text(industry_detail) or ""
    products_text = _normalize_text(products_detail) or ""
    name_text = _normalize_text(company_name) or ""
    combined = " ".join(part for part in (industry_text, products_text, name_text) if part).lower()
    if not combined:
        return "Unknown"

    if _contains_any(combined, ("전기업", "가스", "배관공급", "냉·온수", "증기", "수도업", "하수", "폐기물 처리")):
        return "유틸리티"
    if _contains_any(combined, ("금융", "보험", "은행", "증권", "신탁", "집합투자", "캐피탈", "저축기관", "여신")):
        return "금융"
    if _contains_any(industry_text.lower(), ("부동산",)) or " reit" in f" {combined} ":
        return "부동산"
    if _contains_any(
        industry_text.lower(),
        (
            "정보 서비스",
            "시장조사",
            "여론조사",
            "창작",
            "예술관련 서비스",
            "출판업",
            "오디오물",
            "영상·오디오물 제공 서비스업",
        ),
    ):
        return "커뮤니케이션 서비스"
    if _contains_any(
        industry_text.lower(),
        (
            "소프트웨어",
            "컴퓨터",
            "반도체",
            "전자부품",
            "통신 및 방송 장비",
            "영상 및 음향기기",
            "광학기기",
            "정밀기기",
            "자료처리",
            "시스템 통합",
            "마그네틱 및 광학 매체",
        ),
    ):
        return "정보기술(IT)"
    if _contains_any(
        combined,
        (
            "의약",
            "제약",
            "의료",
            "치과",
            "바이오",
            "헬스",
            "pharma",
            "therapeutic",
            "gene",
            "cell",
        ),
    ):
        return "헬스케어"
    if _contains_any(
        combined,
        (
            "영화",
            "방송",
            "프로그램",
            "광고",
            "출판",
            "오디오물",
            "텔레비전",
            "통신업",
            "포털",
            "인터넷 정보매개",
            "콘텐츠",
            "게임",
            "엔터",
            "미디어",
        ),
    ):
        return "커뮤니케이션 서비스"
    if _contains_any(
        combined,
        (
            "소프트웨어",
            "컴퓨터",
            "반도체",
            "전자부품",
            "통신 및 방송 장비",
            "영상 및 음향기기",
            "광학기기",
            "정밀기기",
            "자료처리",
            "시스템 통합",
            "전자집적",
            " it",
            " ai",
        ),
    ):
        return "정보기술(IT)"
    if _contains_any(
        combined,
        (
            "식품",
            "음료",
            "담배",
            "농업",
            "수산물",
            "육류",
            "곡물",
            "주류",
            "유제품",
            "생활용품",
            "화장품",
            "미용",
            "세제",
            "소매업",
            "편의점",
            "유통",
            "낙농제품",
            "과자",
            "빵",
        ),
    ):
        return "필수소비재"
    if _contains_any(
        combined,
        (
            "자동차",
            "의복",
            "의류",
            "섬유",
            "직물",
            "방적",
            "신발",
            "가죽",
            "화장품",
            "호텔",
            "여행",
            "오락",
            "레저",
            "면세점",
            "백화점",
            "가정용 기기",
            "가구",
            "소매업",
            "유원지",
            "숙박",
            "외식",
        ),
    ):
        return "임의소비재"
    if _contains_any(
        combined,
        (
            "기계",
            "건설",
            "건축",
            "엔지니어링",
            "운송",
            "항공",
            "선박",
            "보트",
            "화물",
            "창고",
            "택배",
            "임대업",
            "도매업",
            "전기 및 통신 공사업",
            "항공기",
            "우주선",
            "방산",
            "무기",
            "물류",
            "전동기",
            "전기장비",
            "케이블",
            "조명장치",
            "사업지원 서비스",
            "시설물 축조",
            "건물설비 설치",
        ),
    ):
        return "산업재"
    if _contains_any(
        combined,
        (
            "화학",
            "철강",
            "금속",
            "비철",
            "시멘트",
            "비금속",
            "유리",
            "요업",
            "플라스틱",
            "고무",
            "종이",
            "펄프",
            "목재",
            "목재",
            "상자",
            "용기",
            "섬유원료",
            "화학섬유",
            "농약",
            "비료",
        ),
    ):
        return "소재"
    if _contains_any(combined, ("석유", "원유", "정제품", "에너지", "연료", "가스전", "탐사", "윤활유")):
        return "에너지"
    return "Unknown"


def _preferred_name_root(value: object) -> str:
    text = _normalize_text(value) or ""
    if not text:
        return ""
    text = text.replace(" ", "")
    text = re.sub(r"\(.*?\)$", "", text)
    text = re.sub(r"(우|우B|우C|1우|2우|3우|우선주|전환)$", "", text)
    text = re.sub(r"\d+우B?$", "", text)
    return text.strip()


def _fill_missing_sector_from_name_family(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    out = frame.copy()
    out["_name_root"] = out["NameKR"].map(_preferred_name_root)

    sector_map: dict[str, str] = {}
    industry_map: dict[str, str] = {}
    for root, group in out.groupby("_name_root"):
        if not root:
            continue
        known_sectors = [str(v) for v in group["Sector"].tolist() if str(v) not in {"", "Unknown", "nan", "None"}]
        known_industries = [str(v) for v in group["Industry"].tolist() if str(v) not in {"", "nan", "None"}]
        if known_sectors:
            sector_map[root] = pd.Series(known_sectors).value_counts().index[0]
        if known_industries:
            industry_map[root] = known_industries[0]

    def _resolved_sector(row: pd.Series) -> object:
        current = str(row["Sector"])
        if current and current != "Unknown" and current != "nan":
            return row["Sector"]
        return sector_map.get(str(row["_name_root"]), row["Sector"])

    def _resolved_industry(row: pd.Series) -> object:
        current = _normalize_text(row["Industry"])
        if current is not None:
            return row["Industry"]
        return industry_map.get(str(row["_name_root"]), row["Industry"])

    out["Sector"] = out.apply(_resolved_sector, axis=1)
    out["Industry"] = out.apply(_resolved_industry, axis=1)
    return out.drop(columns=["_name_root"])


def _apply_sector_overrides(frame: pd.DataFrame, overrides: dict[str, str]) -> pd.DataFrame:
    if frame.empty or not overrides:
        return frame
    out = frame.copy()
    out["Sector"] = out.apply(
        lambda row: overrides.get(_normalize_symbol(row["Symbol"]), row["Sector"]),
        axis=1,
    )
    return out


def _export_unknown_sector_candidates(frame: pd.DataFrame, output_path: Path | None) -> None:
    if output_path is None:
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    unknown = frame[frame["Sector"].fillna("Unknown").astype(str) == "Unknown"].copy()
    review_cols = ["Symbol", "NameKR", "Market", "Sector", "Industry", "ListingDate", "MarketCap"]
    missing = [col for col in review_cols if col not in unknown.columns]
    for col in missing:
        unknown[col] = None
    unknown = unknown[review_cols].sort_values(["Market", "NameKR", "Symbol"]).reset_index(drop=True)
    unknown.to_csv(output_path, index=False, encoding="utf-8")


def _standardize_listing_frame(frame: pd.DataFrame, *, market: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(
            columns=[
                "Symbol",
                "NameKR",
                "NameEN",
                "Market",
                "Sector",
                "Industry",
                "ListingDate",
                "SharesOutstanding",
                "MarketCap",
                "ReferenceSource",
            ]
        )

    symbol_col = _column_name(frame, "symbol", "code", "종목코드")
    if symbol_col is None:
        raise RuntimeError(f"StockListing({market}) response has no symbol/code column")

    name_col = _column_name(frame, "name", "종목명", "회사명")
    market_col = _column_name(frame, "market", "marketid", "시장구분")
    sector_col = _column_name(frame, "sector", "업종", "dept")
    industry_col = _column_name(frame, "industry", "industrycode", "industry_name")
    listing_date_col = _column_name(frame, "listingdate", "listed_date", "상장일")
    shares_col = _column_name(frame, "stocks", "shares", "listedshares", "상장주식수")
    market_cap_col = _column_name(frame, "marcap", "marketcap", "시가총액")

    desc_lookup = _build_desc_lookup(market)
    rows: list[dict[str, object]] = []
    for item in frame.to_dict(orient="records"):
        symbol = _normalize_symbol(item.get(symbol_col))
        if not symbol:
            continue
        desc_item = desc_lookup.get(symbol, {})
        desc_sector = _normalize_text(desc_item.get("Sector"))
        desc_industry = _normalize_text(desc_item.get("Industry"))
        desc_products = _normalize_text(desc_item.get("Products"))
        raw_sector = _normalize_text(item.get(sector_col))
        detailed_industry = desc_industry or _normalize_text(item.get(industry_col))
        if detailed_industry is None and raw_sector is not None and not _looks_like_dept(raw_sector):
            detailed_industry = raw_sector
        normalized_sector = _normalize_krx_sector(
            industry_detail=detailed_industry,
            products_detail=desc_products,
            company_name=item.get(name_col),
        )
        if normalized_sector == "Unknown" and desc_sector and not _looks_like_dept(desc_sector):
            normalized_sector = _normalize_krx_sector(
                industry_detail=desc_sector,
                products_detail=desc_products,
                company_name=item.get(name_col),
            )
        rows.append(
            {
                "Symbol": symbol,
                "NameKR": _normalize_text(item.get(name_col)),
                "NameEN": None,
                "Market": _normalize_text(item.get(market_col), fallback=market) or market,
                "Sector": normalized_sector,
                "Industry": detailed_industry,
                "ListingDate": _normalize_date_text(desc_item.get("ListingDate") or item.get(listing_date_col)),
                "SharesOutstanding": _normalize_number(item.get(shares_col)),
                "MarketCap": _normalize_number(item.get(market_cap_col)),
                "ReferenceSource": f"fdr:{market}+desc",
            }
        )

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    out = out.drop_duplicates(subset=["Symbol"], keep="first").sort_values(["Market", "Symbol"]).reset_index(drop=True)
    return _fill_missing_sector_from_name_family(out)


def build_krx_components_csv(
    *,
    output_path: Path = DEFAULT_COMPONENTS_CSV,
    compact_output_path: Path = DEFAULT_COMPONENTS_COMPACT_CSV,
    unknown_output_path: Path | None = DEFAULT_COMPONENTS_UNKNOWN_CSV,
    sector_overrides_path: Path | None = DEFAULT_SECTOR_OVERRIDES_CSV,
    db_path: Path | str | None = None,
    markets: tuple[str, ...] = DEFAULT_MARKETS,
    insecure_ssl: bool = False,
    ca_bundle: str | None = None,
) -> tuple[pd.DataFrame, str]:
    if fdr is None:
        raise RuntimeError("FinanceDataReader is not installed")
    configure_ssl(insecure_ssl=insecure_ssl, ca_bundle=ca_bundle)

    frames: list[pd.DataFrame] = []
    for market in markets:
        listing = fdr.StockListing(market)
        standardized = _standardize_listing_frame(listing, market=market)
        if not standardized.empty:
            frames.append(standardized)

    if not frames:
        raise RuntimeError("No KRX listing rows were returned")

    merged = (
        pd.concat(frames, axis=0, ignore_index=True)
        .sort_values(["Market", "Symbol"])
        .drop_duplicates(subset=["Symbol"], keep="first")
        .reset_index(drop=True)
    )
    merged = _apply_sector_overrides(merged, _load_sector_overrides(sector_overrides_path))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(output_path, index=False, encoding="utf-8")

    compact = merged[["Symbol", "Market", "Sector"]].copy()
    compact_output_path.parent.mkdir(parents=True, exist_ok=True)
    compact.to_csv(compact_output_path, index=False, encoding="utf-8")
    _export_unknown_sector_candidates(merged, unknown_output_path)

    if db_path is not None:
        init_krx_project_db(db_path=Path(db_path))
        sync_krx_security_snapshot(
            merged.rename(
                columns={
                    "Symbol": "symbol",
                    "NameKR": "name_kr",
                    "NameEN": "name_en",
                    "Market": "market",
                    "Sector": "sector",
                    "Industry": "industry",
                    "ListingDate": "listing_date",
                    "ReferenceSource": "reference_source",
                }
            ),
            as_of_date=pd.Timestamp.today().normalize().strftime("%Y-%m-%d"),
            db_path=db_path,
        )

    return merged, "fdr"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build KOSPI/KOSDAQ components CSV for the separate KRX project.")
    parser.add_argument("--output", default=str(DEFAULT_COMPONENTS_CSV), help="Full components CSV output path")
    parser.add_argument("--compact-output", default=str(DEFAULT_COMPONENTS_COMPACT_CSV), help="Compact components CSV output path")
    parser.add_argument("--unknown-output", default=str(DEFAULT_COMPONENTS_UNKNOWN_CSV), help="Unknown-sector review CSV output path")
    parser.add_argument("--sector-overrides", default=str(DEFAULT_SECTOR_OVERRIDES_CSV), help="Optional symbol-to-sector override CSV path")
    parser.add_argument("--db-path", default="", help="Optional SQLite DB path to sync securities into")
    parser.add_argument("--markets", default="KOSPI,KOSDAQ", help="Comma-separated markets to load")
    parser.add_argument("--ca-bundle", default="", help="CA bundle path")
    parser.add_argument("--insecure-ssl", action="store_true", help="Disable TLS verification for temporary testing")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    markets = tuple(str(item).strip().upper() for item in str(args.markets).split(",") if str(item).strip())
    frame, source = build_krx_components_csv(
        output_path=Path(args.output),
        compact_output_path=Path(args.compact_output),
        unknown_output_path=Path(args.unknown_output) if str(args.unknown_output).strip() else None,
        sector_overrides_path=Path(args.sector_overrides) if str(args.sector_overrides).strip() else None,
        db_path=Path(args.db_path) if str(args.db_path).strip() else None,
        markets=markets or DEFAULT_MARKETS,
        insecure_ssl=bool(args.insecure_ssl),
        ca_bundle=str(args.ca_bundle).strip() or None,
    )
    print(f"source={source}, rows={len(frame)}, output={Path(args.output).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
