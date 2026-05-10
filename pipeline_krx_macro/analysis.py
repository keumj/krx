from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from pipeline_common.notebook_data import (
    fetch_krx_close_prices,
    load_yield_curve_df,
    load_krx_components,
    make_gbm_series,
)
from pipeline_krx.db import load_latest_krx_benchmark_snapshot

from .macro_data_store import read_macro_frame, read_macro_series

try:
    from fredapi import Fred
except Exception:  # pragma: no cover - optional dependency
    Fred = None


DEFAULT_START_DATE = "2006-01-01"
DEFAULT_LOOKBACK_DAYS = 5200


@dataclass
class MacroDashboard:
    as_of_date: str
    regime_label: str
    risk_level: str
    equity_bias: str
    summary: pd.DataFrame
    scores: pd.DataFrame
    indicators: pd.DataFrame
    fred_macro: pd.DataFrame
    macro_pulse: pd.DataFrame
    regime_scenarios: pd.DataFrame
    rates: pd.DataFrame
    rate_diagnostics: pd.DataFrame
    risk_assets: pd.DataFrame
    risk_breadth: pd.DataFrame
    risk_stress: pd.DataFrame
    dollar_commodities: pd.DataFrame
    dollar_sensitivity: pd.DataFrame
    sector_playbook: pd.DataFrame
    sector_attribution: pd.DataFrame
    sources: pd.DataFrame
    market_series: pd.DataFrame
    rate_series: pd.DataFrame
    yield_curve_series: pd.DataFrame
    risk_series: pd.DataFrame
    stress_series: pd.DataFrame
    commodity_series: pd.DataFrame


def _latest_value(series: pd.Series) -> float:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    return float(clean.iloc[-1]) if not clean.empty else np.nan


def _return_pct(series: pd.Series, window: int) -> float:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if len(clean) <= window:
        return np.nan
    base = float(clean.iloc[-(window + 1)])
    latest = float(clean.iloc[-1])
    if base == 0 or not np.isfinite(base):
        return np.nan
    return (latest / base - 1.0) * 100.0


def _return_since_days(series: pd.Series, days: int) -> float:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if len(clean) < 2:
        return np.nan
    if isinstance(clean.index, pd.DatetimeIndex):
        latest_date = clean.index.max()
        base_candidates = clean[clean.index <= latest_date - pd.Timedelta(days=int(days))]
        if base_candidates.empty:
            return np.nan
        base = float(base_candidates.iloc[-1])
    else:
        window = min(int(days), len(clean) - 1)
        base = float(clean.iloc[-(window + 1)])
    latest = float(clean.iloc[-1])
    if base == 0.0 or not np.isfinite(base):
        return np.nan
    return (latest / base - 1.0) * 100.0


def _annualized_recent_vol(series: pd.Series, periods: int = 12) -> float:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    returns = clean.pct_change(fill_method=None).dropna().tail(int(periods))
    if len(returns) < 3:
        return np.nan
    return float(returns.std(ddof=1) * np.sqrt(12.0) * 100.0)


def _return_pct_periods(series: pd.Series, periods: int) -> float:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if len(clean) <= periods:
        return np.nan
    base = float(clean.iloc[-(periods + 1)])
    latest = float(clean.iloc[-1])
    if base == 0 or not np.isfinite(base):
        return np.nan
    return (latest / base - 1.0) * 100.0


def _change_periods(series: pd.Series, periods: int) -> float:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if len(clean) <= periods:
        return np.nan
    return float(clean.iloc[-1] - clean.iloc[-(periods + 1)])


def _latest_date(series: pd.Series) -> str:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if clean.empty:
        return "-"
    return pd.Timestamp(clean.index[-1]).strftime("%Y-%m-%d")


def _change(series: pd.Series, window: int) -> float:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    if len(clean) <= window:
        return np.nan
    return float(clean.iloc[-1] - clean.iloc[-(window + 1)])


def _change_bp(series: pd.Series, window: int) -> float:
    change = _change(series, window)
    return change * 100.0 if np.isfinite(change) else np.nan


def _fmt_level(value: float, suffix: str = "%") -> str:
    return "-" if not np.isfinite(value) else f"{value:.2f}{suffix}"


def _fmt_bp(value: float) -> str:
    return "-" if not np.isfinite(value) else f"{value:+.0f}bp"


def _fmt_signed(value: float, suffix: str = "", ndigits: int = 2) -> str:
    return "-" if not np.isfinite(value) else f"{value:+,.{ndigits}f}{suffix}"


def _rate_move_comment(change_20d: float, change_60d: float) -> str:
    if (np.isfinite(change_20d) and change_20d >= 15.0) or (np.isfinite(change_60d) and change_60d >= 30.0):
        return "최근 금리 상승 속도가 빨라 할인율 부담이 커지는 구간입니다."
    if (np.isfinite(change_20d) and change_20d <= -15.0) or (np.isfinite(change_60d) and change_60d <= -30.0):
        return "최근 금리가 내려가며 밸류에이션 부담은 완화되는 구간입니다."
    return "최근 변화폭은 크지 않아 레벨 자체와 커브 형태를 더 중시합니다."


def _spread_state(value: float) -> str:
    bp = value * 100.0 if np.isfinite(value) else np.nan
    if not np.isfinite(bp):
        return "판단 보류"
    if bp <= -75.0:
        return "깊은 역전"
    if bp < 0.0:
        return "역전"
    if bp < 50.0:
        return "낮은 양의 스프레드"
    return "가파른 정상 커브"


def _stock_rate_comment(name: str, level: float, change_20d: float, change_60d: float, *, dgs2_level: float, dgs10_level: float) -> str:
    move = _rate_move_comment(change_20d, change_60d)
    curve_note = "2Y가 10Y보다 높아 정책 부담이 장기 성장 기대를 누르는 역전 환경입니다." if dgs2_level > dgs10_level else "10Y가 2Y보다 높아 커브는 정상 형태에 가깝습니다."
    if name == "2Y":
        level_note = "정책 부담이 높은 편" if level >= 4.5 else "정책 부담이 중립 이하"
        decision = "주식은 공격적 베타보다 퀄리티, 현금흐름, 방어적 성장주를 우선합니다." if level >= 4.5 or dgs2_level > dgs10_level else "성장주와 장기 듀레이션 종목의 반등 여지를 더 열어둘 수 있습니다."
        return f"현재 2Y는 {_fmt_level(level)}로 {level_note}입니다. 20D {_fmt_bp(change_20d)}, 60D {_fmt_bp(change_60d)}. {move} {curve_note} {decision}"
    if name == "10Y":
        level_note = "주식 할인율 부담이 높은 편" if level >= 4.25 else "할인율 부담이 과도하게 높지는 않은 편"
        decision = "10Y가 오르는 동안에는 고PER 성장주 비중을 서두르기보다 가치주, 금융, 에너지, 현금흐름 우량주를 상대적으로 선호합니다." if level >= 4.25 or change_20d > 10.0 else "10Y가 안정되면 성장주와 배당주의 멀티플 회복 가능성을 봅니다."
        return f"현재 10Y는 {_fmt_level(level)}로 {level_note}입니다. 20D {_fmt_bp(change_20d)}, 60D {_fmt_bp(change_60d)}. {move} {decision}"
    level_note = "초장기 금리 부담이 높은 편" if level >= 4.5 else "초장기 금리는 중립권"
    decision = "30Y가 높거나 상승하면 장기채 성격의 배당주, REITs, 유틸리티에는 부담이고, 인플레 방어력이 있는 업종을 함께 봅니다." if level >= 4.5 or change_20d > 10.0 else "30Y 안정은 장기 듀레이션 주식과 방어적 배당주의 상대 매력을 높입니다."
    return f"현재 30Y는 {_fmt_level(level)}로 {level_note}입니다. 20D {_fmt_bp(change_20d)}, 60D {_fmt_bp(change_60d)}. {move} {decision}"


def _stock_spread_comment(name: str, level: float, change_20d: float, change_60d: float) -> str:
    current_bp = level * 100.0 if np.isfinite(level) else np.nan
    state = _spread_state(level)
    widening = (np.isfinite(change_20d) and change_20d >= 15.0) or (np.isfinite(change_60d) and change_60d >= 30.0)
    flattening = (np.isfinite(change_20d) and change_20d <= -15.0) or (np.isfinite(change_60d) and change_60d <= -30.0)
    if name == "10Y-3M":
        decision = "주식은 경기민감주보다 퀄리티, 필수소비, 헬스케어, 현금비중을 우선합니다." if current_bp < 0.0 else "침체 신호가 완화되는 쪽이라 금융과 경기민감주를 점진적으로 재검토할 수 있습니다."
    elif name == "10Y-2Y":
        decision = "역전 상태에서는 지수 베타 확대를 서두르기보다 방어와 우량 성장주 중심이 낫습니다." if current_bp < 0.0 else "정상화가 진행되면 은행, 산업재, 가치주 쪽 로테이션 가능성을 봅니다."
    elif name == "5Y-2Y":
        decision = "중기 구간도 눌려 있어 정책 부담이 이어지는 그림입니다. 중소형 경기민감주보다 이익 안정성이 높은 종목을 우선합니다." if current_bp < 0.0 else "중기 성장 기대가 살아나는 쪽이라 경기 회복 민감 업종을 일부 열어둘 수 있습니다."
    else:
        decision = "초장기 프리미엄 확대는 장기금리 민감 업종에 부담입니다." if widening and current_bp > 0.0 else "초장기 스프레드 안정은 배당주, 유틸리티, REITs 부담 완화로 해석합니다."
    direction = "스프레드가 확대 중이라 커브 정상화 쪽 신호가 있습니다." if widening else "스프레드가 축소 중이라 커브 플래트닝 압력이 있습니다." if flattening else "최근 스프레드 변화는 제한적입니다."
    return f"현재 {name}는 {_fmt_bp(current_bp)}로 {state} 상태입니다. 20D {_fmt_bp(change_20d)}, 60D {_fmt_bp(change_60d)}. {direction} {decision}"


def _zscore(series: pd.Series, window: int = 252) -> float:
    clean = pd.to_numeric(series, errors="coerce").dropna().tail(window)
    if len(clean) < 20:
        return np.nan
    std = float(clean.std(ddof=1))
    if std <= 0 or not np.isfinite(std):
        return np.nan
    return float((clean.iloc[-1] - clean.mean()) / std)


def _score_from_z(z: float, *, inverse: bool = False) -> float:
    if not np.isfinite(z):
        return 50.0
    score = 50.0 + np.clip(z, -2.0, 2.0) * 20.0
    if inverse:
        score = 100.0 - score
    return float(np.clip(score, 0.0, 100.0))


def _score_state(value: float, *, high: str, mid: str = "중립", low: str) -> str:
    if not np.isfinite(value):
        return "판단 보류"
    if value >= 65.0:
        return high
    if value >= 45.0:
        return mid
    return low


def _percentile_rank(series: pd.Series, window: int = 504) -> float:
    clean = pd.to_numeric(series, errors="coerce").dropna().tail(window)
    if len(clean) < 20:
        return np.nan
    latest = float(clean.iloc[-1])
    return float((clean <= latest).mean() * 100.0)


def _load_kospi200_components(max_symbols: int = 200) -> tuple[pd.DataFrame, str]:
    benchmark = load_latest_krx_benchmark_snapshot("KOSPI200")
    if benchmark is not None and not benchmark.empty and "symbol" in benchmark.columns:
        out = benchmark.copy()
        out["Symbol"] = out["symbol"].astype(str).str.strip().str.upper()
        out["Sector"] = out.get("sector", "Unknown")
        if "benchmark_weight" in out.columns:
            out["benchmark_weight"] = pd.to_numeric(out["benchmark_weight"], errors="coerce")
        return out.head(max_symbols).reset_index(drop=True), "sqlite:benchmark_constituents:KOSPI200"

    components, source = load_krx_components(max_symbols=max_symbols)
    return components, f"{source}:fallback_for_KOSPI200"


def _cap_weighted_series(
    prices: pd.DataFrame,
    symbols: list[str],
    *,
    weights: pd.Series | None = None,
    name: str = "KOSPI200",
) -> pd.Series:
    frame = prices.reindex(columns=symbols).apply(pd.to_numeric, errors="coerce").dropna(how="all")
    if frame.empty:
        return pd.Series(dtype=float, name=name)
    daily = frame.pct_change(fill_method=None)
    if weights is not None and not weights.empty:
        aligned = pd.to_numeric(weights.reindex(frame.columns), errors="coerce").fillna(0.0)
        if float(aligned.sum()) > 0.0:
            valid_weight = daily.notna().mul(aligned, axis=1).sum(axis=1).replace(0.0, np.nan)
            market_daily = daily.mul(aligned, axis=1).sum(axis=1, min_count=1).div(valid_weight).dropna()
        else:
            market_daily = daily.mean(axis=1, skipna=True).dropna()
    else:
        market_daily = daily.mean(axis=1, skipna=True).dropna()
    if market_daily.empty:
        return pd.Series(dtype=float, name=name)
    return (1.0 + market_daily).cumprod().rename(name)


def _read_local_series(path: Path, name: str, start: str) -> pd.Series | None:
    if not path.is_file():
        return None
    try:
        raw = pd.read_csv(path)
    except Exception:
        return None
    if raw.empty:
        return None
    cols = {str(c).strip().lower(): c for c in raw.columns}
    date_col = cols.get("date") or cols.get("datetime") or raw.columns[0]
    value_col = (
        cols.get("close")
        or cols.get("adj close")
        or cols.get("adj_close")
        or cols.get("price")
        or cols.get("value")
        or raw.columns[-1]
    )
    out = raw[[date_col, value_col]].copy()
    out.columns = ["date", "value"]
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    out = out.dropna().sort_values("date")
    if out.empty:
        return None
    series = pd.Series(out["value"].values, index=out["date"].dt.normalize(), name=name)
    series = series[~series.index.duplicated(keep="last")]
    series = series[series.index >= pd.Timestamp(start)]
    return series.dropna() if not series.dropna().empty else None


def _load_local_or_fallback(
    name: str,
    *,
    env_name: str,
    filenames: list[str],
    start: str,
    base: float,
    drift: float,
    vol: float,
    seed: int,
) -> tuple[pd.Series, str]:
    sqlite_series, sqlite_source = read_macro_series(name, start_date=start)
    if sqlite_series is not None and not sqlite_series.empty:
        sqlite_series.name = name
        return sqlite_series, sqlite_source or "macro_sqlite"
    candidates = [os.getenv(env_name, "").strip(), *filenames]
    for raw_path in candidates:
        if not raw_path:
            continue
        series = _read_local_series(Path(raw_path), name, start)
        if series is not None:
            return series, f"local_csv:{raw_path}"
    return make_gbm_series(name, start=start, base=base, drift=drift, vol=vol, seed=seed, min_periods=1000), "fallback"


def _make_mean_reverting_series(
    name: str,
    *,
    start: str,
    base: float,
    mean: float,
    speed: float,
    vol: float,
    seed: int,
) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start=start, end=pd.Timestamp.today().normalize())
    if len(idx) < 260:
        idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=1000)
    values = np.empty(len(idx), dtype=float)
    values[0] = base
    for i in range(1, len(idx)):
        shock = rng.normal(0.0, vol)
        values[i] = values[i - 1] + speed * (mean - values[i - 1]) + shock
    values = np.clip(values, 0.0, None)
    return pd.Series(values, index=idx, name=name)


def _load_fred_or_local_series(
    name: str,
    *,
    fred_id: str,
    sqlite_id: str | None = None,
    env_name: str,
    filenames: list[str],
    start: str,
    fallback_base: float,
    fallback_mean: float,
    fallback_speed: float,
    fallback_vol: float,
    seed: int,
) -> tuple[pd.Series, str]:
    sqlite_series, sqlite_source = read_macro_series(sqlite_id or fred_id, start_date=start)
    if sqlite_series is not None and not sqlite_series.empty:
        sqlite_series.name = name
        return sqlite_series, sqlite_source or "macro_sqlite"
    candidates = [os.getenv(env_name, "").strip(), *filenames]
    for raw_path in candidates:
        if not raw_path:
            continue
        series = _read_local_series(Path(raw_path), name, start)
        if series is not None:
            return series.rename(name), f"local_csv:{raw_path}"
    if Fred is not None:
        try:
            fred = Fred(api_key=os.getenv("FRED_API_KEY"))
            raw = fred.get_series(fred_id, observation_start=start)
            series = pd.Series(raw).dropna().astype(float)
            series.index = pd.to_datetime(series.index).normalize()
            series = series[~series.index.duplicated(keep="last")]
            series = series[series.index >= pd.Timestamp(start)].rename(name)
            if not series.empty:
                return series, f"fred:{fred_id}"
        except Exception:
            pass
    return (
        _make_mean_reverting_series(
            name,
            start=start,
            base=fallback_base,
            mean=fallback_mean,
            speed=fallback_speed,
            vol=fallback_vol,
            seed=seed,
        ),
        "fallback",
    )


def _load_preferred_macro_series(
    name: str,
    *,
    sqlite_ids: list[str],
    env_name: str,
    filenames: list[str],
    start: str,
    fallback_base: float,
    fallback_mean: float,
    fallback_speed: float,
    fallback_vol: float,
    seed: int,
    fred_id: str | None = None,
) -> tuple[pd.Series, str]:
    for sqlite_id in sqlite_ids:
        sqlite_series, sqlite_source = read_macro_series(sqlite_id, start_date=start)
        if sqlite_series is not None and not sqlite_series.empty:
            sqlite_series.name = name
            source = sqlite_source or "macro_sqlite"
            return sqlite_series, f"{source}:{sqlite_id}"
    candidates = [os.getenv(env_name, "").strip(), *filenames]
    for raw_path in candidates:
        if not raw_path:
            continue
        series = _read_local_series(Path(raw_path), name, start)
        if series is not None:
            return series.rename(name), f"local_csv:{raw_path}"
    if fred_id and Fred is not None:
        try:
            fred = Fred(api_key=os.getenv("FRED_API_KEY"))
            raw = fred.get_series(fred_id, observation_start=start)
            series = pd.Series(raw).dropna().astype(float)
            series.index = pd.to_datetime(series.index).normalize()
            series = series[~series.index.duplicated(keep="last")]
            series = series[series.index >= pd.Timestamp(start)].rename(name)
            if not series.empty:
                return series, f"fred:{fred_id}"
        except Exception:
            pass
    return (
        _make_mean_reverting_series(
            name,
            start=start,
            base=fallback_base,
            mean=fallback_mean,
            speed=fallback_speed,
            vol=fallback_vol,
            seed=seed,
        ),
        "fallback",
    )


def _simple_regime(growth: float, inflation: float, policy: float, risk: float) -> tuple[str, str, str]:
    if risk < 35 and growth < 45:
        return "Risk-off Slowdown", "High", "Defensive"
    if growth >= 55 and inflation < 60 and risk >= 50:
        return "Goldilocks / Risk-on", "Low", "Constructive"
    if inflation >= 65 and policy >= 60:
        return "Inflation Pressure / Tight Policy", "Medium-High", "Selective"
    if growth < 45 and inflation < 55:
        return "Disinflation Slowdown", "Medium", "Quality"
    if risk >= 60:
        return "Liquidity-led Risk-on", "Medium", "Constructive"
    return "Mixed Macro", "Medium", "Neutral"


def _sector_bias_table(growth: float, inflation: float, policy: float, risk: float, liquidity: float) -> pd.DataFrame:
    rows = [
        ("반도체/IT", "수출·유동성·위험선호 민감", 0.30 * growth + 0.30 * risk + 0.25 * liquidity + 0.15 * (100 - policy), "국내 증시 비중이 큰 성장/수출 축입니다. 소비자심리와 KOSPI200 모멘텀이 좋고 환율·금리가 안정될수록 점수가 올라갑니다."),
        ("자동차/수출제조", "원화·글로벌 수요·제조 사이클 민감", 0.40 * growth + 0.25 * risk + 0.20 * liquidity + 0.15 * (100 - inflation), "산업생산과 수출 경기, 원화 방향의 영향을 함께 받습니다. 성장 점수가 살아나고 비용 압력이 낮을 때 우호적으로 봅니다."),
        ("금융", "커브·금리 레벨·경기 민감", 0.35 * growth + 0.30 * policy + 0.20 * risk + 0.15 * liquidity, "은행과 보험은 금리 레벨과 경기 신용 흐름에 민감합니다. 커브가 정상화되고 위험선호가 버틸 때 상대 매력이 좋아집니다."),
        ("화학/소재", "제조업·비용 압력 민감", 0.45 * growth + 0.20 * risk + 0.20 * (100 - inflation) + 0.15 * liquidity, "원자재 가격 자체보다 국내 산업생산, 수입 원가, 환율 부담을 함께 봅니다. 비용 압력이 낮아질수록 마진 회복 여지가 커집니다."),
        ("경기소비/유통", "소매판매·고용·금리 민감", 0.35 * growth + 0.25 * risk + 0.25 * (100 - policy) + 0.15 * (100 - inflation), "소매판매와 고용이 개선되고 금리 부담이 낮아질 때 우호적입니다. 물가가 높으면 실질 구매력이 눌릴 수 있습니다."),
        ("필수소비/음식료", "방어·원가 안정 민감", 0.15 * growth + 0.20 * risk + 0.45 * (100 - inflation) + 0.20 * (100 - policy), "수요 방어력은 높지만 원가와 환율 부담에 취약합니다. 물가 압력이 낮아질수록 방어 매력이 선명해집니다."),
        ("바이오/헬스케어", "금리 하락·위험선호 민감", 0.20 * growth + 0.30 * risk + 0.35 * (100 - policy) + 0.15 * liquidity, "장기 성장 기대가 커 금리와 유동성에 민감합니다. 정책 부담이 낮아지고 시장 위험선호가 살아날 때 점수가 올라갑니다."),
        ("통신/유틸/배당", "방어·금리 민감", 0.10 * growth + 0.20 * risk + 0.50 * (100 - policy) + 0.20 * (100 - inflation), "방어적 현금흐름과 배당 성격이 강합니다. 금리와 물가 부담이 내려갈 때 상대 매력이 개선됩니다."),
    ]
    out = pd.DataFrame(rows, columns=["섹터", "민감도", "선호 점수", "읽는 법"])
    out["선호 점수"] = out["선호 점수"].astype(float).clip(0.0, 100.0)
    out["의견"] = pd.cut(
        out["선호 점수"],
        bins=[-0.1, 40, 60, 100],
        labels=["비중축소 후보", "중립", "비중확대 후보"],
    ).astype(str)
    return out.sort_values("선호 점수", ascending=False).reset_index(drop=True)


def _macro_pulse_table(
    scores_map: dict[str, float],
    *,
    risk_return_20d: float,
    realized_vol_20d: float,
    curve_10y_2y: pd.Series,
    dgs2: pd.Series,
    dgs10: pd.Series,
    dxy: pd.Series,
    dxy_label: str,
    domestic_basket: pd.Series,
) -> pd.DataFrame:
    growth = scores_map.get("성장 모멘텀", np.nan)
    inflation = scores_map.get("물가/비용 압력", np.nan)
    policy = scores_map.get("정책 긴축도", np.nan)
    risk = scores_map.get("위험선호", np.nan)
    recession = scores_map.get("침체 리스크", np.nan)
    liquidity = scores_map.get("유동성", np.nan)
    domestic_60d = _return_since_days((1.0 + domestic_basket).cumprod(), 60)
    curve_bp = _latest_value(curve_10y_2y) * 100.0
    rows = [
        {
            "축": "성장 모멘텀",
            "점수": growth,
            "상태": _score_state(growth, high="확장 우위", low="둔화 우위"),
            "핵심 판독": f"KOSPI200 20D {_fmt_signed(risk_return_20d, '%')}, 10Y-2Y {_fmt_bp(curve_bp)}",
            "포트폴리오 의미": "KOSPI200 모멘텀, 소비심리·소매판매 흐름, 장단기 커브가 동시에 개선되는지 보는 축입니다. 높으면 국내 경기민감주와 퀄리티 성장주를 함께 검토합니다.",
        },
        {
            "축": "물가/비용 압력",
            "점수": inflation,
            "상태": _score_state(inflation, high="비용 압력 높음", low="디스인플레 우위"),
            "핵심 판독": f"국내 비용 바스켓 60D {_fmt_signed(domestic_60d, '%')}, 10Y {_fmt_level(_latest_value(dgs10))}",
            "포트폴리오 의미": "CPI, 원/달러 환율, 장기금리가 만드는 국내 비용 압력입니다. 높으면 기업 마진이 눌릴 수 있어 가격 전가력과 원가 민감도를 먼저 확인합니다.",
        },
        {
            "축": "정책 긴축도",
            "점수": policy,
            "상태": _score_state(policy, high="긴축 부담", low="완화 여지"),
            "핵심 판독": f"2Y {_fmt_level(_latest_value(dgs2))}, 20D {_fmt_bp(_change_bp(dgs2, 20))}",
            "포트폴리오 의미": "2년 금리는 시장이 보는 정책금리 경로에 가깝습니다. 긴축도가 높으면 고PER·장기 듀레이션 주식의 멀티플 리스크를 보수적으로 보고 현금흐름 가시성을 중시합니다.",
        },
        {
            "축": "위험선호",
            "점수": risk,
            "상태": _score_state(risk, high="리스크 온", low="리스크 오프"),
            "핵심 판독": f"20D 변동성 {_fmt_level(realized_vol_20d, '%/년')}",
            "포트폴리오 의미": "수익률, 낙폭, 변동성을 묶어 시장이 위험을 받아들이는 정도를 봅니다. 낮으면 지수 베타 확대보다 손실 방어, 현금흐름, 변동성 관리가 우선입니다.",
        },
        {
            "축": "침체 리스크",
            "점수": recession,
            "상태": _score_state(recession, high="침체 경계", low="침체 압력 낮음"),
            "핵심 판독": f"10Y-2Y {_fmt_bp(curve_bp)}",
            "포트폴리오 의미": "커브 역전과 주가 약세를 함께 보는 둔화 경계 지표입니다. 높으면 경기민감 매출과 레버리지 부담이 큰 업종에서 이익 추정 하향 리스크를 먼저 점검합니다.",
        },
        {
            "축": "유동성",
            "점수": liquidity,
            "상태": _score_state(liquidity, high="유동성 우호", low="유동성 부담"),
            "핵심 판독": f"{dxy_label} 60D {_fmt_signed(_return_pct(dxy, 60), '%')}, 10Y 60D {_fmt_bp(_change_bp(dgs10, 60))}",
            "포트폴리오 의미": "환율·달러 지표와 장기금리가 안정될수록 국내 위험자산 여건은 부드러워집니다. 높으면 KOSPI200 위험선호에 우호적이고, 낮으면 환율·할인율 부담을 경계합니다.",
        },
    ]
    return pd.DataFrame(rows)


def _regime_scenario_table(
    growth: float,
    inflation: float,
    policy: float,
    risk: float,
    recession: float,
    liquidity: float,
) -> pd.DataFrame:
    current = np.array([growth, inflation, policy, risk, recession, liquidity], dtype=float)
    scenarios = [
        ("Goldilocks / Risk-on", [72, 35, 35, 72, 28, 70], "성장과 유동성은 우호적이고 물가·정책 부담은 낮은 조합입니다. 주식에는 가장 편한 환경이지만, 과열 여부는 변동성과 폭 지표로 확인해야 합니다.", "퀄리티 성장, 산업재, 경기민감 소비재를 열어두되 급등 후 추격매수보다는 breadth가 동반되는지 확인합니다."),
        ("Inflation Pressure / Tight Policy", [55, 82, 78, 45, 55, 35], "물가와 정책 부담이 동시에 높은 조합입니다. 명목 성장률은 버틸 수 있어도 할인율과 비용 압력이 멀티플을 누르는 국면입니다.", "가격 전가력, 에너지, 가치주, 현금흐름 우량주를 우선하고 고PER 장기 성장주는 선별적으로 봅니다."),
        ("Risk-off Slowdown", [32, 50, 62, 30, 78, 32], "성장과 위험선호가 꺾이고 침체 신호가 강한 조합입니다. 지수 반등이 나와도 이익 전망과 신용 리스크가 확인되기 전까지는 방어적으로 해석합니다.", "방어주, 헬스케어, 필수소비, 현금 비중을 높여 보고 경기민감 베타 확대는 늦춥니다."),
        ("Disinflation Relief", [45, 32, 45, 58, 42, 65], "물가 압력이 낮아지고 유동성이 개선되는 조합입니다. 성장 자체가 강하지 않아도 할인율 완화만으로 듀레이션 자산이 회복될 수 있습니다.", "장기 듀레이션 성장주와 배당주의 회복 가능성을 점검하되, 경기 둔화가 이익을 훼손하는지 같이 봅니다."),
        ("Liquidity-led Risk-on", [55, 45, 35, 82, 35, 82], "성장 확신보다 유동성이 위험선호를 밀어 올리는 조합입니다. 가격은 강할 수 있지만 펀더멘털 확인이 늦으면 변동성 재확대에 취약합니다.", "모멘텀은 활용하되 손절 기준과 리밸런싱 규칙을 분명히 두고 breadth 약화를 경계합니다."),
    ]
    rows = []
    for name, template, description, tilt in scenarios:
        template_arr = np.array(template, dtype=float)
        valid = np.isfinite(current)
        distance = np.sqrt(np.mean((current[valid] - template_arr[valid]) ** 2)) if valid.any() else np.nan
        proximity = np.clip(100.0 - distance, 0.0, 100.0) if np.isfinite(distance) else np.nan
        rows.append(
            {
                "시나리오": name,
                "근접도": proximity,
                "핵심 조건": description,
                "전략 기울기": tilt,
            }
        )
    return pd.DataFrame(rows).sort_values("근접도", ascending=False).reset_index(drop=True)


def _rate_diagnostics_table(
    yields: pd.DataFrame,
    *,
    dgs2: pd.Series,
    dgs3m: pd.Series,
    dgs5: pd.Series,
    dgs10: pd.Series,
    dgs30: pd.Series,
    curve_10y_2y: pd.Series,
    curve_10y_3m: pd.Series,
    curve_30y_10y: pd.Series,
) -> pd.DataFrame:
    curve_level = yields.reindex(columns=["DGS2", "DGS5", "DGS10", "DGS30"]).mean(axis=1, skipna=True).rename("curve_level")
    front_policy = (dgs2 - dgs3m).dropna().rename("2Y-3M")
    belly = ((2.0 * dgs5) - dgs2 - dgs10).dropna().rename("5Y_belly")
    rows = [
        {
            "진단": "커브 레벨",
            "현재": _latest_value(curve_level),
            "백분위": _percentile_rank(curve_level),
            "해석": "2Y·5Y·10Y·30Y 평균으로 본 전체 금리 레벨입니다. 백분위가 높으면 주식의 할인율 부담이 크다는 뜻이라, 특히 고PER 성장주와 장기 현금흐름 자산의 밸류에이션을 보수적으로 봅니다.",
        },
        {
            "진단": "프론트엔드 정책 압력",
            "현재": _latest_value(front_policy) * 100.0,
            "백분위": _percentile_rank(front_policy),
            "해석": "2Y-3M 스프레드는 시장이 가까운 정책금리와 향후 2년 경로를 어떻게 보는지 보여줍니다. 높으면 금리 인하 기대가 약한 편이고, 낮거나 음수이면 완화 기대가 더 많이 가격에 반영된 상태입니다.",
        },
        {
            "진단": "10Y-2Y 경기 기울기",
            "현재": _latest_value(curve_10y_2y) * 100.0,
            "백분위": _percentile_rank(curve_10y_2y),
            "해석": "10Y-2Y는 대표적인 장단기 경기 기울기입니다. 음수이거나 백분위가 낮으면 단기 정책금리가 장기 성장 기대를 누르는 환경이라 경기 둔화와 은행 마진 압력을 함께 봅니다.",
        },
        {
            "진단": "5Y 벨리 곡률",
            "현재": _latest_value(belly) * 100.0,
            "백분위": _percentile_rank(belly),
            "해석": "5Y 벨리는 중기 금리가 양 끝인 2Y·10Y 대비 얼마나 비싼지 보는 곡률 지표입니다. 높으면 중기 구간에 정책 불확실성이나 공급 부담이 몰린 것으로 해석할 수 있습니다.",
        },
        {
            "진단": "30Y-10Y 장기 프리미엄",
            "현재": _latest_value(curve_30y_10y) * 100.0,
            "백분위": _percentile_rank(curve_30y_10y),
            "해석": "30Y-10Y는 초장기 프리미엄의 대용치입니다. 상승하면 재정·인플레·장기채 공급 부담이 커졌다는 신호일 수 있어 REITs, 유틸리티, 배당주처럼 장기금리 민감 업종에 부담입니다.",
        },
        {
            "진단": "10Y-3M 침체 신호",
            "현재": _latest_value(curve_10y_3m) * 100.0,
            "백분위": _percentile_rank(curve_10y_3m),
            "해석": "10Y-3M은 단기 정책금리와 장기 성장 기대의 차이를 봅니다. 깊은 역전은 과거 경기 둔화 구간에서 자주 나타났기 때문에, 위험자산 비중 확대 전에 신용·고용·이익 지표 확인이 필요합니다.",
        },
    ]
    return pd.DataFrame(rows)


def _risk_breadth_table(prices: pd.DataFrame, spx: pd.Series) -> pd.DataFrame:
    frame = prices.apply(pd.to_numeric, errors="coerce").dropna(axis=1, how="all")
    if frame.empty:
        return pd.DataFrame()
    latest = frame.ffill().iloc[-1]
    ret20 = frame.pct_change(20, fill_method=None).iloc[-1] * 100.0
    ret60 = frame.pct_change(60, fill_method=None).iloc[-1] * 100.0
    ma20 = frame.rolling(20).mean().iloc[-1]
    ma60 = frame.rolling(60).mean().iloc[-1]
    ma200 = frame.rolling(200).mean().iloc[-1]
    def pct_valid(mask: pd.Series) -> float:
        clean = mask.dropna()
        return float(clean.mean() * 100.0) if not clean.empty else np.nan

    daily = frame.pct_change(fill_method=None).dropna(how="all")
    market_daily = spx.pct_change(fill_method=None).dropna()
    aligned = daily.reindex(market_daily.index).dropna(how="all")
    down_days = market_daily.reindex(aligned.index) < 0
    downside_participation = pct_valid((aligned[down_days] < 0).mean(axis=1)) if down_days.any() else np.nan
    rows = [
        {
            "진단": "20D 상승 종목 비율",
            "현재": pct_valid(ret20 > 0),
            "해석": "최근 20거래일 수익률이 플러스인 종목 비율입니다. 지수는 오르는데 이 비율이 낮으면 소수 대형주 주도 장세일 수 있어 반등의 지속성을 낮게 봅니다.",
        },
        {
            "진단": "60D 상승 종목 비율",
            "현재": pct_valid(ret60 > 0),
            "해석": "최근 60거래일 기준 상승 종목 비율로, 중기 위험선호가 시장 전체로 퍼졌는지 확인합니다. 50% 아래면 지수 상승이 넓게 확산되지 않았다는 뜻입니다.",
        },
        {
            "진단": "20D 이동평균 상회",
            "현재": pct_valid(latest > ma20),
            "해석": "현재 가격이 20일 이동평균 위에 있는 종목 비율입니다. 단기 매수세가 얼마나 넓게 살아났는지 보여주며, 급락 후 회복 초입을 확인하는 데 유용합니다.",
        },
        {
            "진단": "60D 이동평균 상회",
            "현재": pct_valid(latest > ma60),
            "해석": "현재 가격이 60일 이동평균 위에 있는 종목 비율입니다. 20일 지표보다 느리지만 노이즈가 적어, 반등이 단기 기술적 반등을 넘어 중기 추세로 이어지는지 봅니다.",
        },
        {
            "진단": "200D 이동평균 상회",
            "현재": pct_valid(latest > ma200),
            "해석": "현재 가격이 200일 이동평균 위에 있는 종목 비율입니다. 장기 상승 추세의 기반을 보는 지표라 낮을수록 위험자산 온도를 낮추고 방어적 해석을 강화합니다.",
        },
        {
            "진단": "20D 수익률 분산",
            "현재": float(ret20.dropna().std(ddof=1)) if ret20.dropna().size > 1 else np.nan,
            "해석": "20일 수익률의 종목 간 표준편차입니다. 높으면 시장 전체보다 업종·팩터·종목 선택이 성과를 더 크게 좌우하는 장세입니다.",
        },
        {
            "진단": "하락일 동반 하락률",
            "현재": downside_participation,
            "해석": "KOSPI200 proxy가 하락한 날에 같이 하락한 종목 비율입니다. 높으면 매도 압력이 시장 전반으로 퍼진다는 뜻이라 손실 확대와 상관관계 상승에 대비합니다.",
        },
    ]
    return pd.DataFrame(rows)


def _stress_state(name: str, level: float, change_20d: float, percentile: float) -> str:
    if not np.isfinite(level):
        return "판단 보류"
    if name == "VIX":
        if level >= 30.0 or percentile >= 85.0:
            return "공포 확대"
        if level >= 20.0 or change_20d >= 5.0:
            return "경계"
        return "안정"
    if level >= 6.0 or percentile >= 85.0:
        return "신용 스트레스"
    if level >= 3.5 or change_20d >= 0.5:
        return "경계"
    return "안정"


def _risk_stress_table(
    vix: pd.Series,
    ig_oas: pd.Series,
    hy_oas: pd.Series,
    baa_aaa: pd.Series,
    *,
    krx_vol: pd.Series | None = None,
    krx_drawdown: pd.Series | None = None,
    fx_vol: pd.Series | None = None,
    curve_10y_2y: pd.Series | None = None,
) -> pd.DataFrame:
    if krx_vol is not None or krx_drawdown is not None or fx_vol is not None or curve_10y_2y is not None:
        rows = []
        specs = [
            (
                "KOSPI200 20D 실현변동성",
                krx_vol if krx_vol is not None else pd.Series(dtype=float),
                "연 %",
                "KOSPI200 자체 가격 변동성입니다. 높아지면 같은 주식 비중에서도 손익 흔들림이 커지므로 포지션 크기와 손실 한도를 낮춰 봅니다.",
            ),
            (
                "KOSPI200 낙폭",
                krx_drawdown if krx_drawdown is not None else pd.Series(dtype=float),
                "%",
                "최근 고점 대비 하락률입니다. 낙폭이 깊고 회복 폭이 좁으면 시장 내부 스트레스가 아직 남아 있다고 봅니다.",
            ),
            (
                "USD/KRW 20D 변동성",
                fx_vol if fx_vol is not None else pd.Series(dtype=float),
                "연 %",
                "원/달러 환율 변동성입니다. 확대되면 외국인 수급과 수입 원가, 헤지 비용 부담이 커질 수 있습니다.",
            ),
            (
                "국고채 10Y-2Y",
                curve_10y_2y if curve_10y_2y is not None else pd.Series(dtype=float),
                "%p",
                "국내 장단기 금리차입니다. 낮거나 역전되면 단기 정책 부담이 장기 성장 기대를 누르는 환경으로 해석합니다.",
            ),
        ]
        for name, series, unit, note in specs:
            level = _latest_value(series)
            change_20d = _change(series, 20)
            change_60d = _change(series, 60)
            pct = _percentile_rank(series)
            stress_level = level if name != "KOSPI200 낙폭" else abs(level)
            rows.append(
                {
                    "지표": name,
                    "현재": level,
                    "단위": unit,
                    "20D 변화": change_20d,
                    "60D 변화": change_60d,
                    "백분위": pct,
                    "상태": "스트레스 확대" if np.isfinite(pct) and pct >= 80.0 or np.isfinite(stress_level) and stress_level >= 25.0 else "안정" if np.isfinite(pct) and pct <= 50.0 else "경계",
                    "해석": note,
                }
            )
        return pd.DataFrame(rows)

    rows = []
    specs = [
        (
            "VIX",
            vix,
            "포인트",
            "옵션시장이 반영하는 향후 30일 기대 변동성입니다. 상승하면 투자자들이 하락 방어 비용을 더 지불한다는 뜻이라, 주가가 버티더라도 포지션 크기와 손실 한도를 보수적으로 둡니다.",
        ),
        (
            "투자등급 회사채 OAS",
            ig_oas,
            "%p",
            "우량 회사채가 국채 대비 요구받는 추가 금리입니다. 상승하면 조달비용과 신용 경계가 커진다는 뜻이라, 레버리지 높은 기업과 장기채 성격 업종을 더 신중하게 봅니다.",
        ),
        (
            "하이일드 OAS",
            hy_oas,
            "%p",
            "저신용 회사채의 추가 보상 요구입니다. 주식시장보다 먼저 경기·유동성 스트레스를 반영하는 경우가 많아, 급등하면 경기민감주와 중소형 베타 노출을 낮춰 봅니다.",
        ),
        (
            "Baa-AAA 신용 프리미엄",
            baa_aaa,
            "%p",
            "중하위 투자등급과 최우량 회사채 간 스프레드 차이입니다. 확대되면 시장이 같은 투자등급 안에서도 신용 퀄리티를 더 엄격히 구분한다는 뜻입니다.",
        ),
    ]
    for name, series, unit, note in specs:
        level = _latest_value(series)
        change_20d = _change(series, 20)
        change_60d = _change(series, 60)
        pct = _percentile_rank(series)
        rows.append(
            {
                "지표": name,
                "현재": level,
                "단위": unit,
                "20D 변화": change_20d,
                "60D 변화": change_60d,
                "백분위": pct,
                "상태": _stress_state("VIX" if name == "VIX" else "Credit", level, change_20d, pct),
                "해석": note,
            }
        )
    return pd.DataFrame(rows)


def _macro_status(value: float, *, high: float, low: float, high_label: str, mid_label: str, low_label: str) -> str:
    if not np.isfinite(value):
        return "판단 보류"
    if value >= high:
        return high_label
    if value <= low:
        return low_label
    return mid_label


def _fred_macro_table(
    *,
    unrate: pd.Series,
    payrolls: pd.Series,
    cpi: pd.Series,
    core_pce: pd.Series,
    indpro: pd.Series,
    retail_sales: pd.Series,
    housing_starts: pd.Series,
    consumer_sentiment: pd.Series,
    m2: pd.Series,
    fedfunds: pd.Series,
    real_gdp: pd.Series,
) -> pd.DataFrame:
    payroll_3m = pd.to_numeric(payrolls, errors="coerce").dropna().diff().tail(3).mean()
    unrate_3m = _change_periods(unrate, 3)
    cpi_yoy = _return_pct_periods(cpi, 12)
    core_pce_yoy = _return_pct_periods(core_pce, 12)
    indpro_yoy = _return_pct_periods(indpro, 12)
    retail_yoy = _return_pct_periods(retail_sales, 12)
    housing_yoy = _return_pct_periods(housing_starts, 12)
    m2_yoy = _return_pct_periods(m2, 12)
    fedfunds_6m = _change_periods(fedfunds, 6)
    gdp_yoy = _return_pct_periods(real_gdp, 4)
    sentiment_level = _latest_value(consumer_sentiment)
    sentiment_pct = _percentile_rank(consumer_sentiment)

    rows = [
        {
            "영역": "고용",
            "지표": "실업률(UNRATE)",
            "현재": _latest_value(unrate),
            "모멘텀": unrate_3m,
            "상태": "고용 둔화 경계" if np.isfinite(unrate_3m) and unrate_3m >= 0.3 else "고용 안정" if np.isfinite(unrate_3m) and unrate_3m <= 0.1 else "완만한 둔화",
            "최근 관측일": _latest_date(unrate),
            "해석": "실업률은 경기 둔화가 노동시장으로 번지는지 보는 가장 직관적인 지표입니다. 3개월 변화가 빠르게 오르면 소비와 기업 이익 전망을 보수적으로 봅니다.",
        },
        {
            "영역": "고용",
            "지표": "비농업 고용(PAYEMS)",
            "현재": _latest_value(payrolls),
            "모멘텀": payroll_3m,
            "상태": _macro_status(payroll_3m, high=150.0, low=50.0, high_label="고용 창출 견조", mid_label="고용 둔화", low_label="고용 약화"),
            "최근 관측일": _latest_date(payrolls),
            "해석": "최근 3개월 평균 월간 고용 증가폭입니다. 고용 증가가 둔화되면 임금·소비 모멘텀도 식을 수 있어 경기민감 업종의 이익 기대를 낮춰 봅니다.",
        },
        {
            "영역": "물가",
            "지표": "CPI YoY",
            "현재": cpi_yoy,
            "모멘텀": _change_periods(cpi.pct_change(12, fill_method=None) * 100.0, 3),
            "상태": _macro_status(cpi_yoy, high=3.5, low=2.5, high_label="물가 부담", mid_label="완만한 둔화", low_label="목표권 근접"),
            "최근 관측일": _latest_date(cpi),
            "해석": "소비자물가의 전년 대비 상승률입니다. 높으면 금리 인하 기대가 후퇴하고, 낮아지면 할인율 부담 완화와 실질소득 개선 가능성이 커집니다.",
        },
        {
            "영역": "물가",
            "지표": "Core PCE YoY",
            "현재": core_pce_yoy,
            "모멘텀": _change_periods(core_pce.pct_change(12, fill_method=None) * 100.0, 3),
            "상태": _macro_status(core_pce_yoy, high=3.0, low=2.3, high_label="근원물가 부담", mid_label="완만한 둔화", low_label="목표권 근접"),
            "최근 관측일": _latest_date(core_pce),
            "해석": "Fed가 특히 중시하는 근원 PCE 물가입니다. 끈적하면 정책금리 부담이 오래가고, 둔화가 확인되면 장기 듀레이션 자산에 우호적입니다.",
        },
        {
            "영역": "생산",
            "지표": "산업생산 YoY",
            "현재": indpro_yoy,
            "모멘텀": _change_periods(indpro.pct_change(12, fill_method=None) * 100.0, 3),
            "상태": _macro_status(indpro_yoy, high=1.0, low=-1.0, high_label="생산 확장", mid_label="정체", low_label="생산 둔화"),
            "최근 관측일": _latest_date(indpro),
            "해석": "제조·광업·유틸리티 생산의 실물 경기 흐름입니다. 약해지면 소재·산업재·운송 같은 경기민감 업종의 매출 추정에 부담입니다.",
        },
        {
            "영역": "소비",
            "지표": "소매판매 YoY",
            "현재": retail_yoy,
            "모멘텀": _change_periods(retail_sales.pct_change(12, fill_method=None) * 100.0, 3),
            "상태": _macro_status(retail_yoy, high=3.0, low=0.0, high_label="소비 견조", mid_label="소비 둔화", low_label="소비 약화"),
            "최근 관측일": _latest_date(retail_sales),
            "해석": "가계 소비의 명목 매출 흐름입니다. 물가를 감안해야 하지만, 둔화가 뚜렷하면 소비재·유통·여행 관련 업종의 매출 민감도를 점검합니다.",
        },
        {
            "영역": "주택",
            "지표": "주택착공 YoY",
            "현재": housing_yoy,
            "모멘텀": _change_periods(housing_starts.pct_change(12, fill_method=None) * 100.0, 3),
            "상태": _macro_status(housing_yoy, high=5.0, low=-5.0, high_label="주택 회복", mid_label="중립", low_label="주택 약화"),
            "최근 관측일": _latest_date(housing_starts),
            "해석": "금리 민감도가 큰 주택 경기의 선행적 수요 지표입니다. 약하면 건설, 주택개량, 지역은행 신용 흐름에 부담이 될 수 있습니다.",
        },
        {
            "영역": "심리",
            "지표": "미시간 소비심리",
            "현재": sentiment_level,
            "모멘텀": _change_periods(consumer_sentiment, 3),
            "상태": "심리 개선" if np.isfinite(sentiment_pct) and sentiment_pct >= 60.0 else "심리 약화" if np.isfinite(sentiment_pct) and sentiment_pct <= 35.0 else "중립",
            "최근 관측일": _latest_date(consumer_sentiment),
            "해석": "가계가 경기와 물가를 어떻게 느끼는지 보여주는 심리 지표입니다. 실제 소비보다 먼저 꺾이거나 회복될 수 있어 소비주 해석의 보조 신호로 씁니다.",
        },
        {
            "영역": "유동성",
            "지표": "M2 YoY",
            "현재": m2_yoy,
            "모멘텀": _change_periods(m2.pct_change(12, fill_method=None) * 100.0, 3),
            "상태": _macro_status(m2_yoy, high=5.0, low=0.0, high_label="유동성 확장", mid_label="낮은 유동성", low_label="유동성 위축"),
            "최근 관측일": _latest_date(m2),
            "해석": "광의통화 증가율입니다. 유동성 확장은 위험자산에 우호적일 수 있고, 위축은 달러 강세·신용경색과 결합될 때 방어적 해석을 강화합니다.",
        },
        {
            "영역": "정책",
            "지표": "Fed Funds",
            "현재": _latest_value(fedfunds),
            "모멘텀": fedfunds_6m * 100.0 if np.isfinite(fedfunds_6m) else np.nan,
            "상태": "정책 완화 진행" if np.isfinite(fedfunds_6m) and fedfunds_6m <= -0.25 else "정책 긴축/고금리 유지" if np.isfinite(fedfunds_6m) and fedfunds_6m >= 0.25 else "정책 정체",
            "최근 관측일": _latest_date(fedfunds),
            "해석": "실제 연방기금금리 레벨과 6개월 변화입니다. 레벨이 높고 내려오지 않으면 할인율 부담이 지속되고, 인하가 시작되면 성장주와 부채 부담 업종의 숨통이 트일 수 있습니다.",
        },
        {
            "영역": "성장",
            "지표": "실질 GDP YoY",
            "현재": gdp_yoy,
            "모멘텀": _change_periods(real_gdp.pct_change(4, fill_method=None) * 100.0, 1),
            "상태": _macro_status(gdp_yoy, high=2.0, low=0.5, high_label="성장 견조", mid_label="성장 둔화", low_label="저성장 경계"),
            "최근 관측일": _latest_date(real_gdp),
            "해석": "분기 실질 GDP의 전년 대비 성장률입니다. 느리지만 거시 레짐의 기준점 역할을 하며, 시장가격 신호가 과하게 앞서갔는지 확인하는 앵커로 씁니다.",
        },
    ]
    return pd.DataFrame(rows)


def _korea_macro_table(
    *,
    unrate: pd.Series,
    payrolls: pd.Series,
    cpi: pd.Series,
    core_pce: pd.Series,
    indpro: pd.Series,
    retail_sales: pd.Series,
    housing_starts: pd.Series,
    consumer_sentiment: pd.Series,
    m2: pd.Series,
    fedfunds: pd.Series,
    real_gdp: pd.Series,
) -> pd.DataFrame:
    employed_3m = pd.to_numeric(payrolls, errors="coerce").dropna().diff().tail(3).mean()
    unrate_3m = _change_periods(unrate, 3)
    cpi_yoy = _return_pct_periods(cpi, 12)
    core_cpi_yoy = _return_pct_periods(core_pce, 12)
    indpro_yoy = _return_pct_periods(indpro, 12)
    retail_yoy = _return_pct_periods(retail_sales, 12)
    m2_yoy = _return_pct_periods(m2, 12)
    policy_6m = _change_periods(fedfunds, 6)
    gdp_yoy = _return_pct_periods(real_gdp, 4)
    sentiment_level = _latest_value(consumer_sentiment)
    sentiment_pct = _percentile_rank(consumer_sentiment)

    rows = [
        {
            "영역": "고용",
            "지표": "한국 실업률",
            "현재": _latest_value(unrate),
            "모멘텀": unrate_3m,
            "상태": "고용 둔화 경계" if np.isfinite(unrate_3m) and unrate_3m >= 0.3 else "고용 안정" if np.isfinite(unrate_3m) and unrate_3m <= 0.1 else "완만한 둔화",
            "최근 관측일": _latest_date(unrate),
            "해석": "한국 노동시장의 둔화 여부를 확인합니다. 실업률 상승 속도가 빨라지면 내수와 기업 이익 전망을 더 보수적으로 봅니다.",
        },
        {
            "영역": "고용",
            "지표": "취업자 수",
            "현재": _latest_value(payrolls),
            "모멘텀": employed_3m,
            "상태": _macro_status(employed_3m, high=50.0, low=0.0, high_label="고용 증가 견조", mid_label="고용 둔화", low_label="고용 약화"),
            "최근 관측일": _latest_date(payrolls),
            "해석": "최근 3개월 평균 취업자 수 증감입니다. 증가세가 둔화되면 소비와 서비스 업종의 매출 민감도를 점검합니다.",
        },
        {
            "영역": "물가",
            "지표": "한국 CPI YoY",
            "현재": cpi_yoy,
            "모멘텀": _change_periods(cpi.pct_change(12, fill_method=None) * 100.0, 3),
            "상태": _macro_status(cpi_yoy, high=3.0, low=2.0, high_label="물가 부담", mid_label="완만한 둔화", low_label="목표권 근접"),
            "최근 관측일": _latest_date(cpi),
            "해석": "한국 소비자물가의 전년 대비 흐름입니다. 높으면 한국은행 완화 기대가 늦어지고, 낮아지면 내수와 금리 민감 업종에 우호적입니다.",
        },
        {
            "영역": "물가",
            "지표": "근원/대체 CPI YoY",
            "현재": core_cpi_yoy,
            "모멘텀": _change_periods(core_pce.pct_change(12, fill_method=None) * 100.0, 3),
            "상태": _macro_status(core_cpi_yoy, high=3.0, low=2.0, high_label="근원물가 부담", mid_label="완만한 둔화", low_label="목표권 근접"),
            "최근 관측일": _latest_date(core_pce),
            "해석": "한국 근원물가 시리즈가 있으면 근원 흐름을, 없으면 CPI 대체값을 사용합니다. 끈적하면 금리 인하 기대를 낮춰 봅니다.",
        },
        {
            "영역": "생산",
            "지표": "한국 산업생산 YoY",
            "현재": indpro_yoy,
            "모멘텀": _change_periods(indpro.pct_change(12, fill_method=None) * 100.0, 3),
            "상태": _macro_status(indpro_yoy, high=2.0, low=-1.0, high_label="생산 확장", mid_label="정체", low_label="생산 둔화"),
            "최근 관측일": _latest_date(indpro),
            "해석": "제조업과 산업 활동의 실물 모멘텀입니다. 약해지면 반도체·소재·산업재의 경기 민감 이익 추정에 부담입니다.",
        },
        {
            "영역": "소비",
            "지표": "한국 소매판매 YoY",
            "현재": retail_yoy,
            "모멘텀": _change_periods(retail_sales.pct_change(12, fill_method=None) * 100.0, 3),
            "상태": _macro_status(retail_yoy, high=3.0, low=0.0, high_label="소비 견조", mid_label="소비 둔화", low_label="소비 약화"),
            "최근 관측일": _latest_date(retail_sales),
            "해석": "내수 소비 흐름입니다. 둔화가 뚜렷하면 유통·음식료·여행·소비재 종목의 매출 민감도를 점검합니다.",
        },
        {
            "영역": "심리",
            "지표": "소비/내수 심리 대체",
            "현재": sentiment_level,
            "모멘텀": _change_periods(consumer_sentiment, 3),
            "상태": "심리 개선" if np.isfinite(sentiment_pct) and sentiment_pct >= 60.0 else "심리 약화" if np.isfinite(sentiment_pct) and sentiment_pct <= 35.0 else "중립",
            "최근 관측일": _latest_date(consumer_sentiment),
            "해석": "소비심리 시리즈가 있으면 심리 지표를, 없으면 소매판매 대체값을 사용합니다. 내수주의 방향성을 보조적으로 확인합니다.",
        },
        {
            "영역": "유동성",
            "지표": "한국 M2 YoY",
            "현재": m2_yoy,
            "모멘텀": _change_periods(m2.pct_change(12, fill_method=None) * 100.0, 3),
            "상태": _macro_status(m2_yoy, high=5.0, low=2.0, high_label="유동성 확장", mid_label="낮은 유동성", low_label="유동성 위축"),
            "최근 관측일": _latest_date(m2),
            "해석": "한국 광의통화 증가율입니다. 유동성이 확장되면 위험자산에 우호적일 수 있고, 위축되면 신용과 내수 민감 업종을 보수적으로 봅니다.",
        },
        {
            "영역": "정책",
            "지표": "한국 기준금리",
            "현재": _latest_value(fedfunds),
            "모멘텀": policy_6m * 100.0 if np.isfinite(policy_6m) else np.nan,
            "상태": "정책 완화 진행" if np.isfinite(policy_6m) and policy_6m <= -0.25 else "정책 긴축/고금리 유지" if np.isfinite(policy_6m) and policy_6m >= 0.25 else "정책 정체",
            "최근 관측일": _latest_date(fedfunds),
            "해석": "한국은행 기준금리 레벨과 6개월 변화입니다. 레벨이 높고 내려오지 않으면 부채 부담 업종과 고PER 성장주를 보수적으로 봅니다.",
        },
        {
            "영역": "성장",
            "지표": "한국 실질 GDP YoY",
            "현재": gdp_yoy,
            "모멘텀": _change_periods(real_gdp.pct_change(4, fill_method=None) * 100.0, 1),
            "상태": _macro_status(gdp_yoy, high=2.5, low=1.0, high_label="성장 견조", mid_label="성장 둔화", low_label="저성장 경계"),
            "최근 관측일": _latest_date(real_gdp),
            "해석": "한국 실질 GDP 전년 대비 성장률입니다. 느리지만 국내 경기 레짐의 기준점으로 사용합니다.",
        },
    ]
    return pd.DataFrame(rows)


def _dollar_sensitivity_table(dxy: pd.Series, commodity_series: pd.DataFrame, spx: pd.Series, *, fx_label: str) -> pd.DataFrame:
    fx_ret = dxy.pct_change(fill_method=None).dropna().rename(fx_label)
    assets = pd.concat([spx.rename("KOSPI200"), commodity_series], axis=1)
    rows = []
    for name in assets.columns:
        asset_ret = assets[name].pct_change(fill_method=None).dropna()
        aligned = pd.concat([fx_ret, asset_ret.rename(name)], axis=1).dropna().tail(120)
        if len(aligned) < 30:
            corr_60d = beta = np.nan
        else:
            recent = aligned.tail(60)
            corr_60d = float(recent[fx_label].corr(recent[name]))
            var = float(recent[fx_label].var(ddof=1))
            beta = float(recent[fx_label].cov(recent[name]) / var) if var > 0 and np.isfinite(var) else np.nan
        if np.isfinite(beta) and beta <= -0.4:
            note = "최근 구간에서 환율·달러 지표가 오를 때 이 자산은 대체로 약했습니다. 같은 흐름이 이어지면 수익률 훼손 가능성을 먼저 봅니다."
        elif np.isfinite(beta) and beta >= 0.4:
            note = "최근 구간에서 환율·달러 지표와 같은 방향으로 움직였습니다. 일반적인 역상관보다 안전자산 수요나 고유 수급 요인이 더 컸을 가능성이 있습니다."
        else:
            note = "최근 60거래일 기준 환율 민감도는 제한적이거나 방향이 뚜렷하지 않습니다. 이 경우 해당 자산의 자체 수급과 금리 변수를 더 봅니다."
        rows.append(
            {
                "대상": name,
                "60D 상관": corr_60d,
                "환율 베타": beta,
                "60D 수익률": _return_pct(assets[name], 60),
                "해석": note,
            }
        )
    return pd.DataFrame(rows)


def _sector_attribution_table(growth: float, inflation: float, policy: float, risk: float, liquidity: float) -> pd.DataFrame:
    specs = [
        ("반도체/IT", 0.30 * growth, 0.0, 0.15 * (100 - policy) + 0.25 * liquidity, 0.30 * risk, "환율 급등과 금리 재상승이 겹치면 외국인 수급과 고PER 멀티플이 같이 흔들릴 수 있습니다."),
        ("자동차/수출제조", 0.40 * growth, 0.15 * (100 - inflation), 0.20 * liquidity, 0.25 * risk, "원화 약세 자체는 매출 환산에 우호적일 수 있지만 비용과 수요 둔화가 동반되면 마진 해석이 나빠집니다."),
        ("금융", 0.35 * growth, 0.0, 0.30 * policy + 0.15 * liquidity, 0.20 * risk, "커브가 눌리고 위험선호가 약하면 순이자마진보다 대손 우려가 더 크게 반영될 수 있습니다."),
        ("화학/소재", 0.45 * growth, 0.20 * (100 - inflation), 0.15 * liquidity, 0.20 * risk, "산업생산이 둔화되거나 환율·원가 부담이 커지면 스프레드 회복이 늦어질 수 있습니다."),
        ("경기소비/유통", 0.35 * growth, 0.15 * (100 - inflation), 0.25 * (100 - policy), 0.25 * risk, "물가와 금리가 높으면 실질소득과 소비 여력이 줄어 매출 모멘텀이 약해질 수 있습니다."),
        ("필수소비/음식료", 0.15 * growth, 0.45 * (100 - inflation), 0.20 * (100 - policy), 0.20 * risk, "원재료·환율 부담이 다시 커지면 방어주라도 마진 압박을 받을 수 있습니다."),
        ("바이오/헬스케어", 0.20 * growth, 0.0, 0.35 * (100 - policy) + 0.15 * liquidity, 0.30 * risk, "금리 재상승과 위험선호 약화가 겹치면 장기 성장 기대의 할인율 부담이 커집니다."),
        ("통신/유틸/배당", 0.10 * growth, 0.20 * (100 - inflation), 0.50 * (100 - policy), 0.20 * risk, "금리가 다시 오르면 배당수익률 매력과 차입비용 측면에서 부담이 커질 수 있습니다."),
    ]
    rows = []
    for sector, growth_c, inflation_c, policy_c, risk_c, risk_note in specs:
        rows.append(
            {
                "섹터": sector,
                "성장 기여": growth_c,
                "물가/비용 기여": inflation_c,
                "정책/금리 기여": policy_c,
                "위험선호 기여": risk_c,
                "선호 점수": growth_c + inflation_c + policy_c + risk_c,
                "주요 리스크": risk_note,
            }
        )
    return pd.DataFrame(rows).sort_values("선호 점수", ascending=False).reset_index(drop=True)


def build_macro_dashboard(
    *,
    start_date: str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> MacroDashboard:
    start = start_date or DEFAULT_START_DATE
    lookback = max(int(lookback_days), 126)
    tail_n = max(lookback + 60, 260)

    yield_ids = ["DGS1MO", "DGS3MO", "DGS6MO", "DGS1", "DGS2", "DGS3", "DGS5", "DGS7", "DGS10", "DGS20", "DGS30"]
    korea_yield_ids = ["KR_BASE_RATE", "KR_TBOND_3Y", "KR_TBOND_10Y", "KR_TBOND_30Y"]
    korea_yields, korea_yield_source = read_macro_frame(korea_yield_ids, start_date=start)
    korea_rates_active = korea_yields is not None and not korea_yields.empty
    if korea_rates_active:
        kr = korea_yields.ffill().bfill()
        def _kr_col(name: str) -> pd.Series:
            series = kr.get(name)
            return pd.Series(np.nan, index=kr.index, name=name) if series is None else series

        base = _kr_col("KR_BASE_RATE")
        tbond3 = _kr_col("KR_TBOND_3Y")
        tbond10 = _kr_col("KR_TBOND_10Y")
        tbond30 = _kr_col("KR_TBOND_30Y")
        yields = pd.DataFrame(index=kr.index)
        yields["DGS1MO"] = base
        yields["DGS3MO"] = base
        yields["DGS6MO"] = base
        yields["DGS1"] = base
        yields["DGS2"] = tbond3
        yields["DGS3"] = tbond3
        yields["DGS5"] = pd.concat([tbond3, tbond10], axis=1).mean(axis=1, skipna=True)
        yields["DGS7"] = pd.concat([tbond3, tbond10], axis=1).mean(axis=1, skipna=True)
        yields["DGS10"] = tbond10
        yields["DGS20"] = pd.concat([tbond10, tbond30], axis=1).mean(axis=1, skipna=True)
        yields["DGS30"] = tbond30.combine_first(tbond10)
        yields = yields.reindex(columns=yield_ids).ffill().bfill()
        yield_source = korea_yield_source or "macro_sqlite:korea_rates"
    else:
        sqlite_yields, sqlite_yield_source = read_macro_frame(yield_ids, start_date=start)
        if sqlite_yields is not None and not sqlite_yields.empty:
            yields, yield_source = sqlite_yields.ffill().bfill(), sqlite_yield_source or "macro_sqlite"
        else:
            yields, yield_source = load_yield_curve_df(yield_ids, start=start)
    yields = yields.tail(tail_n).copy()

    equity_label = "KOSPI200"
    components, components_source = _load_kospi200_components(max_symbols=200)
    symbols = components["Symbol"].astype(str).head(200).tolist()
    prices, price_source = fetch_krx_close_prices(symbols, start)
    prices = prices.tail(tail_n).copy()
    weights = (
        components.set_index("Symbol")["benchmark_weight"]
        if "benchmark_weight" in components.columns
        else None
    )
    proxy_spx = _cap_weighted_series(prices, symbols, weights=weights, name=equity_label)
    kospi200_series, kospi200_source = read_macro_series("KOSPI200", start_date=start)
    if kospi200_series is not None and not kospi200_series.empty:
        spx = kospi200_series.tail(tail_n).rename(equity_label)
        equity_source = kospi200_source or "macro_sqlite:KOSPI200"
    else:
        spx = proxy_spx
        equity_source = f"{price_source}:weighted_proxy"

    dxy, dxy_source = _load_preferred_macro_series(
        "USD/KRW",
        sqlite_ids=["KR_USDKRW", "DXY"],
        env_name="USDKRW_CSV_PATH",
        filenames=["data/korea_usdkrw.csv", "data/usdkrw.csv", "data/dxy.csv", "data/DXY.csv"],
        start=start,
        fallback_base=1350.0,
        fallback_mean=1320.0,
        fallback_speed=0.010,
        fallback_vol=5.5,
        seed=7,
    )
    fx_label = "USD/KRW" if ("KR_USDKRW" in dxy_source or "korea_usdkrw" in dxy_source or dxy_source == "fallback") else "DXY"
    vix = pd.Series(dtype=float, name="VIX")
    ig_oas = pd.Series(dtype=float, name="Investment Grade OAS")
    hy_oas = pd.Series(dtype=float, name="High Yield OAS")
    baa_spread = pd.Series(dtype=float, name="Baa-10Y Spread")
    aaa_spread = pd.Series(dtype=float, name="Aaa-10Y Spread")
    unrate, unrate_source = _load_preferred_macro_series(
        "Korea Unemployment Rate",
        sqlite_ids=["KR_UNRATE", "UNRATE"],
        env_name="UNRATE_CSV_PATH",
        filenames=["data/korea_unemployment_rate.csv", "data/unrate.csv", "data/UNRATE.csv"],
        start=start,
        fallback_base=4.0,
        fallback_mean=4.2,
        fallback_speed=0.030,
        fallback_vol=0.035,
        seed=41,
        fred_id="UNRATE",
    )
    payrolls, payrolls_source = _load_preferred_macro_series(
        "Korea Employed Persons",
        sqlite_ids=["KR_EMPLOYED", "PAYEMS"],
        env_name="PAYEMS_CSV_PATH",
        filenames=["data/korea_employed.csv", "data/payems.csv", "data/nonfarm_payrolls.csv"],
        start=start,
        fallback_base=28500.0,
        fallback_mean=28700.0,
        fallback_speed=0.004,
        fallback_vol=18.0,
        seed=42,
        fred_id="PAYEMS",
    )
    cpi, cpi_source = _load_preferred_macro_series(
        "Korea CPI",
        sqlite_ids=["KR_CPI", "CPIAUCSL"],
        env_name="CPI_CSV_PATH",
        filenames=["data/korea_cpi.csv", "data/cpi.csv", "data/CPIAUCSL.csv"],
        start=start,
        fallback_base=113.0,
        fallback_mean=116.0,
        fallback_speed=0.004,
        fallback_vol=0.10,
        seed=43,
        fred_id="CPIAUCSL",
    )
    core_pce, core_pce_source = _load_preferred_macro_series(
        "Korea Core CPI",
        sqlite_ids=["KR_CORE_CPI", "KR_CPI", "PCEPILFE"],
        env_name="CORE_PCE_CSV_PATH",
        filenames=["data/korea_core_cpi.csv", "data/korea_cpi.csv", "data/core_pce.csv", "data/PCEPILFE.csv"],
        start=start,
        fallback_base=112.0,
        fallback_mean=115.0,
        fallback_speed=0.004,
        fallback_vol=0.08,
        seed=44,
        fred_id="PCEPILFE",
    )
    indpro, indpro_source = _load_preferred_macro_series(
        "Korea Industrial Production",
        sqlite_ids=["KR_IPI", "INDPRO"],
        env_name="INDPRO_CSV_PATH",
        filenames=["data/korea_industrial_production.csv", "data/indpro.csv", "data/industrial_production.csv"],
        start=start,
        fallback_base=103.0,
        fallback_mean=104.0,
        fallback_speed=0.010,
        fallback_vol=0.08,
        seed=45,
        fred_id="INDPRO",
    )
    retail_sales, retail_source = _load_preferred_macro_series(
        "Korea Retail Sales",
        sqlite_ids=["KR_RETAIL", "RSAFS"],
        env_name="RETAIL_SALES_CSV_PATH",
        filenames=["data/korea_retail_sales.csv", "data/retail_sales.csv", "data/RSAFS.csv"],
        start=start,
        fallback_base=110.0,
        fallback_mean=114.0,
        fallback_speed=0.004,
        fallback_vol=0.30,
        seed=46,
        fred_id="RSAFS",
    )
    housing_starts, housing_source = _load_preferred_macro_series(
        "Korea Construction Starts",
        sqlite_ids=["KR_CONSTRUCTION", "KR_RETAIL", "HOUST"],
        env_name="HOUSING_STARTS_CSV_PATH",
        filenames=["data/korea_construction.csv", "data/korea_retail_sales.csv", "data/houst.csv", "data/housing_starts.csv"],
        start=start,
        fallback_base=100.0,
        fallback_mean=102.0,
        fallback_speed=0.015,
        fallback_vol=0.50,
        seed=47,
        fred_id="HOUST",
    )
    consumer_sentiment, sentiment_source = _load_preferred_macro_series(
        "Korea Consumer Sentiment",
        sqlite_ids=["KR_CCSI", "KR_RETAIL", "UMCSENT"],
        env_name="UMCSENT_CSV_PATH",
        filenames=["data/korea_consumer_sentiment.csv", "data/korea_retail_sales.csv", "data/umcsent.csv", "data/consumer_sentiment.csv"],
        start=start,
        fallback_base=100.0,
        fallback_mean=100.0,
        fallback_speed=0.020,
        fallback_vol=0.55,
        seed=48,
        fred_id="UMCSENT",
    )
    m2, m2_source = _load_preferred_macro_series(
        "Korea M2",
        sqlite_ids=["KR_M2", "M2SL"],
        env_name="M2_CSV_PATH",
        filenames=["data/korea_m2.csv", "data/m2.csv", "data/M2SL.csv"],
        start=start,
        fallback_base=4100.0,
        fallback_mean=4300.0,
        fallback_speed=0.003,
        fallback_vol=6.0,
        seed=49,
        fred_id="M2SL",
    )
    fedfunds, fedfunds_source = _load_preferred_macro_series(
        "Korea Base Rate",
        sqlite_ids=["KR_BASE_RATE", "FEDFUNDS"],
        env_name="FEDFUNDS_CSV_PATH",
        filenames=["data/korea_base_rate.csv", "data/fedfunds.csv", "data/FEDFUNDS.csv"],
        start=start,
        fallback_base=3.5,
        fallback_mean=3.0,
        fallback_speed=0.020,
        fallback_vol=0.025,
        seed=50,
        fred_id="FEDFUNDS",
    )
    real_gdp, gdp_source = _load_preferred_macro_series(
        "Korea Real GDP",
        sqlite_ids=["KR_GDP", "GDPC1"],
        env_name="REAL_GDP_CSV_PATH",
        filenames=["data/korea_real_gdp.csv", "data/gdpc1.csv", "data/real_gdp.csv"],
        start=start,
        fallback_base=560000.0,
        fallback_mean=590000.0,
        fallback_speed=0.003,
        fallback_vol=800.0,
        seed=51,
        fred_id="GDPC1",
    )
    dxy = dxy.tail(tail_n)
    vix = vix.tail(tail_n)
    ig_oas = ig_oas.tail(tail_n)
    hy_oas = hy_oas.tail(tail_n)
    baa_spread = baa_spread.tail(tail_n)
    aaa_spread = aaa_spread.tail(tail_n)
    unrate = unrate.tail(tail_n)
    payrolls = payrolls.tail(tail_n)
    cpi = cpi.tail(tail_n)
    core_pce = core_pce.tail(tail_n)
    indpro = indpro.tail(tail_n)
    retail_sales = retail_sales.tail(tail_n)
    housing_starts = housing_starts.tail(tail_n)
    consumer_sentiment = consumer_sentiment.tail(tail_n)
    m2 = m2.tail(tail_n)
    fedfunds = fedfunds.tail(tail_n)
    real_gdp = real_gdp.tail(tail_n)
    dxy_monthly = dxy.resample("MS").last().dropna() if not dxy.empty else dxy
    domestic_series = pd.concat(
        [
            dxy_monthly.rename(fx_label),
            cpi.rename("CPI"),
            consumer_sentiment.rename("Consumer Sentiment"),
            retail_sales.rename("Retail Sales"),
            m2.rename("M2"),
        ],
        axis=1,
    ).sort_index().ffill().tail(tail_n).dropna(how="all")
    baa_aaa_spread = (baa_spread - aaa_spread).dropna().rename("Baa-AAA Spread")
    dgs2 = yields["DGS2"] if "DGS2" in yields else pd.Series(dtype=float)
    dgs3m = yields["DGS3MO"] if "DGS3MO" in yields else pd.Series(dtype=float)
    dgs5 = yields["DGS5"] if "DGS5" in yields else pd.Series(dtype=float)
    dgs10 = yields["DGS10"] if "DGS10" in yields else pd.Series(dtype=float)
    dgs30 = yields["DGS30"] if "DGS30" in yields else pd.Series(dtype=float)
    curve_10y_3m = (dgs10 - dgs3m).dropna().rename("10Y-3M")
    curve_10y_2y = (dgs10 - dgs2).dropna().rename("10Y-2Y")
    curve_5y_2y = (dgs5 - dgs2).dropna().rename("5Y-2Y")
    curve_30y_10y = (dgs30 - dgs10).dropna().rename("30Y-10Y")

    latest_dates = [idx.max() for idx in [yields.index, prices.index, dxy.index, spx.index, domestic_series.index] if len(idx) > 0]
    as_of = max(latest_dates).strftime("%Y-%m-%d") if latest_dates else "-"

    risk_return_20d = _return_pct(spx, 20)
    risk_return_60d = _return_pct(spx, 60)
    spx_daily = spx.pct_change(fill_method=None).dropna()
    realized_vol_20d = float(spx_daily.tail(20).std(ddof=1) * np.sqrt(252) * 100.0) if len(spx_daily) >= 20 else np.nan
    rolling_vol = (spx_daily.rolling(20).std(ddof=1) * np.sqrt(252) * 100.0).dropna().rename("20D Ann Vol")
    drawdown = ((spx / spx.cummax() - 1.0) * 100.0).dropna().rename("Drawdown")

    domestic_basket = domestic_series.pct_change(fill_method=None).mean(axis=1).dropna()
    cpi_momentum = (cpi.pct_change(12, fill_method=None) * 100.0).dropna()
    sentiment_momentum = (consumer_sentiment.pct_change(12, fill_method=None) * 100.0).dropna()
    retail_momentum = (retail_sales.pct_change(12, fill_method=None) * 100.0).dropna()
    m2_momentum = (m2.pct_change(12, fill_method=None) * 100.0).dropna()
    growth_score = np.nanmean(
        [
            _score_from_z(_zscore(spx_daily.rolling(20).sum().dropna())),
            _score_from_z(_zscore(curve_10y_2y)),
            _score_from_z(_zscore(sentiment_momentum)),
            _score_from_z(_zscore(retail_momentum)),
        ]
    )
    inflation_score = np.nanmean(
        [
            _score_from_z(_zscore(dgs10)),
            _score_from_z(_zscore(dxy)),
            _score_from_z(_zscore(cpi_momentum)),
        ]
    )
    policy_score = np.nanmean([_score_from_z(_zscore(dgs2)), _score_from_z(_zscore(dgs10))])
    vol_penalty = 50.0 if not np.isfinite(realized_vol_20d) else float(np.clip(100.0 - (realized_vol_20d - 8.0) * 3.0, 0.0, 100.0))
    fx_vol = (dxy.pct_change(fill_method=None).dropna().rolling(20).std(ddof=1) * np.sqrt(252) * 100.0).dropna().rename("USD/KRW 20D Ann Vol")
    domestic_stress_score = np.nanmean(
        [
            _score_from_z(_zscore(rolling_vol), inverse=True),
            _score_from_z(_zscore(drawdown)),
            _score_from_z(_zscore(fx_vol), inverse=True),
            _score_from_z(_zscore(curve_10y_2y)),
        ]
    )
    risk_score = np.nanmean([_score_from_z(_zscore(spx)), _score_from_z(_zscore(drawdown)), vol_penalty, domestic_stress_score])
    recession_score = np.nanmean([_score_from_z(_zscore(curve_10y_2y), inverse=True), _score_from_z(_zscore(spx), inverse=True)])
    liquidity_score = np.nanmean([_score_from_z(_zscore(dgs10), inverse=True), _score_from_z(_zscore(dxy), inverse=True), _score_from_z(_zscore(spx))])

    scores_map = {
        "성장 모멘텀": growth_score,
        "물가/비용 압력": inflation_score,
        "정책 긴축도": policy_score,
        "위험선호": risk_score,
        "침체 리스크": recession_score,
        "유동성": liquidity_score,
    }
    regime_label, risk_level, equity_bias = _simple_regime(growth_score, inflation_score, policy_score, risk_score)

    summary = pd.DataFrame(
        [
            {"항목": "거시 국면", "값": regime_label, "해석": "성장, 물가·비용, 정책금리, 장단기 커브, 위험선호를 묶어 현재 시장이 어느 환경에 가까운지 요약합니다. 단일 지표가 아니라 서로 다른 자산군의 신호가 같은 방향인지 보는 출발점입니다."},
            {"항목": "리스크 레벨", "값": risk_level, "해석": "변동성, 낙폭, 성장 둔화 신호를 함께 본 위험 단계입니다. 높을수록 손실 방어와 현금흐름 안정성을 우선하고, 낮을수록 위험자산 확대 여지를 검토합니다."},
            {"항목": "주식 비중 의견", "값": equity_bias, "해석": "현재 거시 조합에서 주식 노출을 공격적으로 둘지, 선별적으로 둘지, 방어적으로 둘지에 대한 상위 가이드입니다. 개별 종목 판단 전 포트폴리오의 기본 기울기를 정하는 용도입니다."},
            {"항목": "기준일", "값": as_of, "해석": "분석에 사용된 로컬 데이터의 최신 관측일입니다. 일부 자산은 휴장일이나 데이터 제공 지연 때문에 기준일이 서로 다를 수 있어, 최신성 확인용으로 표시합니다."},
        ]
    )
    scores = pd.DataFrame([{"점수": key, "값": float(np.clip(value, 0, 100)) if np.isfinite(value) else np.nan} for key, value in scores_map.items()])
    korea_macro_active = any(
        "KR_" in source
        for source in [
            unrate_source,
            payrolls_source,
            cpi_source,
            core_pce_source,
            indpro_source,
            retail_source,
            sentiment_source,
            m2_source,
            fedfunds_source,
            gdp_source,
        ]
    )
    macro_table_builder = _korea_macro_table if korea_macro_active else _fred_macro_table
    fred_macro = macro_table_builder(
        unrate=unrate,
        payrolls=payrolls,
        cpi=cpi,
        core_pce=core_pce,
        indpro=indpro,
        retail_sales=retail_sales,
        housing_starts=housing_starts,
        consumer_sentiment=consumer_sentiment,
        m2=m2,
        fedfunds=fedfunds,
        real_gdp=real_gdp,
    )
    macro_pulse = _macro_pulse_table(
        scores_map,
        risk_return_20d=risk_return_20d,
        realized_vol_20d=realized_vol_20d,
        curve_10y_2y=curve_10y_2y,
        dgs2=dgs2,
        dgs10=dgs10,
        dxy=dxy,
        dxy_label=fx_label,
        domestic_basket=domestic_basket,
    )
    regime_scenarios = _regime_scenario_table(
        growth_score,
        inflation_score,
        policy_score,
        risk_score,
        recession_score,
        liquidity_score,
    )
    indicators = pd.DataFrame(
        [
            {"지표": f"{equity_label} 20D 수익률", "현재": risk_return_20d, "단위": "%", "해석": "최근 한 달 안팎의 위험자산 모멘텀입니다. 플러스이면 단기 매수세가 살아 있다는 뜻이고, 마이너스이면 방어적 포지셔닝이나 현금비중 점검이 필요합니다."},
            {"지표": f"{equity_label} 60D 수익률", "현재": risk_return_60d, "단위": "%", "해석": "분기 단위의 중기 추세를 봅니다. 20D는 좋지만 60D가 약하면 단기 반등일 수 있고, 둘 다 강하면 위험선호가 더 견고하다고 봅니다."},
            {"지표": "20D 연율 변동성", "현재": realized_vol_20d, "단위": "연 %", "해석": "최근 일간 수익률의 흔들림을 연율화한 값입니다. 높을수록 같은 주식 비중이라도 포트폴리오 손익 변동이 커지므로 포지션 크기와 손실 한도를 보수적으로 둡니다."},
            {"지표": "현재 낙폭", "현재": _latest_value(drawdown), "단위": "%", "해석": "최근 고점 대비 얼마나 내려와 있는지입니다. 낙폭이 깊으면 가격 매력은 생길 수 있지만, 회복에는 breadth와 변동성 안정이 같이 필요합니다."},
            {"지표": "10Y-2Y 스프레드", "현재": _latest_value(curve_10y_2y), "단위": "%p", "해석": "장기 성장 기대와 단기 정책 부담의 차이입니다. 음수이면 정책금리가 장기 성장 기대보다 높다는 뜻이라 경기 둔화와 금융주 마진 압력을 함께 경계합니다."},
            {"지표": "국내 비용/활동 바스켓 60D", "현재": _return_since_days((1.0 + domestic_basket).cumprod(), 60), "단위": "%", "해석": "CPI, 원/달러, 소비자심리, 소매판매, M2를 묶어 국내 비용 압력과 수요 활동을 함께 봅니다. 기존 원자재 폴백 차트 대신 실제 한국 DB 시리즈만 사용합니다."},
            {"지표": f"{fx_label} 60D 변화율", "현재": _return_pct(dxy, 60), "단위": "%", "해석": f"환율·달러 지표 상승은 외국인 수급과 수입 원가, 글로벌 유동성 부담으로 이어질 수 있습니다. 안정되면 {equity_label} 위험선호와 마진 부담 완화에 도움이 됩니다."},
        ]
    )
    rates = pd.DataFrame(
        [
            {"구간": "2Y", "현재 금리": _latest_value(dgs2), "20D 변화(bp)": _change_bp(dgs2, 20), "60D 변화(bp)": _change_bp(dgs2, 60), "해석": _stock_rate_comment("2Y", _latest_value(dgs2), _change_bp(dgs2, 20), _change_bp(dgs2, 60), dgs2_level=_latest_value(dgs2), dgs10_level=_latest_value(dgs10))},
            {"구간": "10Y", "현재 금리": _latest_value(dgs10), "20D 변화(bp)": _change_bp(dgs10, 20), "60D 변화(bp)": _change_bp(dgs10, 60), "해석": _stock_rate_comment("10Y", _latest_value(dgs10), _change_bp(dgs10, 20), _change_bp(dgs10, 60), dgs2_level=_latest_value(dgs2), dgs10_level=_latest_value(dgs10))},
            {"구간": "30Y", "현재 금리": _latest_value(dgs30), "20D 변화(bp)": _change_bp(dgs30, 20), "60D 변화(bp)": _change_bp(dgs30, 60), "해석": _stock_rate_comment("30Y", _latest_value(dgs30), _change_bp(dgs30, 20), _change_bp(dgs30, 60), dgs2_level=_latest_value(dgs2), dgs10_level=_latest_value(dgs10))},
            {"구간": "10Y-3M", "현재 금리": _latest_value(curve_10y_3m), "20D 변화(bp)": _change_bp(curve_10y_3m, 20), "60D 변화(bp)": _change_bp(curve_10y_3m, 60), "해석": _stock_spread_comment("10Y-3M", _latest_value(curve_10y_3m), _change_bp(curve_10y_3m, 20), _change_bp(curve_10y_3m, 60))},
            {"구간": "10Y-2Y", "현재 금리": _latest_value(curve_10y_2y), "20D 변화(bp)": _change_bp(curve_10y_2y, 20), "60D 변화(bp)": _change_bp(curve_10y_2y, 60), "해석": _stock_spread_comment("10Y-2Y", _latest_value(curve_10y_2y), _change_bp(curve_10y_2y, 20), _change_bp(curve_10y_2y, 60))},
            {"구간": "5Y-2Y", "현재 금리": _latest_value(curve_5y_2y), "20D 변화(bp)": _change_bp(curve_5y_2y, 20), "60D 변화(bp)": _change_bp(curve_5y_2y, 60), "해석": _stock_spread_comment("5Y-2Y", _latest_value(curve_5y_2y), _change_bp(curve_5y_2y, 20), _change_bp(curve_5y_2y, 60))},
            {"구간": "30Y-10Y", "현재 금리": _latest_value(curve_30y_10y), "20D 변화(bp)": _change_bp(curve_30y_10y, 20), "60D 변화(bp)": _change_bp(curve_30y_10y, 60), "해석": _stock_spread_comment("30Y-10Y", _latest_value(curve_30y_10y), _change_bp(curve_30y_10y, 20), _change_bp(curve_30y_10y, 60))},
        ]
    )
    rate_diagnostics = _rate_diagnostics_table(
        yields,
        dgs2=dgs2,
        dgs3m=dgs3m,
        dgs5=dgs5,
        dgs10=dgs10,
        dgs30=dgs30,
        curve_10y_2y=curve_10y_2y,
        curve_10y_3m=curve_10y_3m,
        curve_30y_10y=curve_30y_10y,
    )
    risk_assets = pd.DataFrame(
        [
            {"자산": equity_label, "20D 수익률": risk_return_20d, "60D 수익률": risk_return_60d, "20D 연율 변동성": realized_vol_20d, "현재 낙폭": _latest_value(drawdown), "판독": "주식 위험선호의 핵심 온도계입니다. 수익률이 플러스이고 변동성과 낙폭이 낮으면 위험자산 확대 여지가 커지고, 반대 조합이면 방어적 해석을 우선합니다."},
            {"자산": fx_label, "20D 수익률": _return_pct(dxy, 20), "60D 수익률": _return_pct(dxy, 60), "20D 연율 변동성": float(dxy.pct_change(fill_method=None).dropna().tail(20).std(ddof=1) * np.sqrt(252) * 100.0), "현재 낙폭": _latest_value((dxy / dxy.cummax() - 1.0) * 100.0), "판독": f"환율·달러 지표는 {equity_label} 외국인 수급과 글로벌 유동성 부담을 읽는 핵심 지표입니다. 상승과 변동성 확대가 겹치면 방어적 해석을 강화합니다."},
        ]
    )
    risk_breadth = _risk_breadth_table(prices, spx)
    risk_stress = _risk_stress_table(
        vix,
        ig_oas,
        hy_oas,
        baa_aaa_spread,
        krx_vol=rolling_vol,
        krx_drawdown=drawdown,
        fx_vol=fx_vol,
        curve_10y_2y=curve_10y_2y,
    )
    domestic_rows = [
        (fx_label, domestic_series[fx_label], f"환율 상승은 외국인 수급 부담, 수입 원가, 헤지 비용으로 이어질 수 있습니다. {equity_label}와 함께 보면 원화 약세가 시장에 부담인지 방어인지 구분하는 데 도움이 됩니다."),
        ("CPI", domestic_series["CPI"], "소비자물가 지수입니다. 전년 대비 상승률과 방향을 함께 보며, 높을수록 소비 여력과 기업 마진에 부담을 줄 수 있습니다."),
        ("Consumer Sentiment", domestic_series["Consumer Sentiment"], "한국 소비자심리지수(CCSI)입니다. 기준선 100 전후와 방향을 함께 보며, 내수·소비 업종의 수요 기대를 읽는 보조 지표로 씁니다."),
        ("Retail Sales", domestic_series["Retail Sales"], "국내 소매판매 흐름입니다. 내수 소비와 경기소비 업종의 기본 온도를 읽는 데 사용합니다."),
        ("M2", domestic_series["M2"], "광의통화 증가 흐름입니다. 국내 유동성 여건을 보는 보조 지표입니다."),
    ]
    dollar_commodities = pd.DataFrame(
        [
            *[
                {
                    "지표": name,
                    "현재": _latest_value(series),
                    "20D 변화율": _return_since_days(series, 20),
                    "60D 변화율": _return_since_days(series, 60),
                    "최근 12개월 변동성": _annualized_recent_vol(series, 12),
                    "해석": note,
                }
                for name, series, note in domestic_rows
            ],
        ]
    )
    dollar_sensitivity = _dollar_sensitivity_table(dxy, domestic_series.drop(columns=[fx_label], errors="ignore"), spx, fx_label=fx_label)
    sector_bias = _sector_bias_table(growth_score, inflation_score, policy_score, risk_score, liquidity_score)
    sector_attribution = _sector_attribution_table(growth_score, inflation_score, policy_score, risk_score, liquidity_score)
    sources = pd.DataFrame(
        [
            {"데이터": f"{equity_label} 구성", "출처": components_source},
            {"데이터": f"{equity_label} 지수", "출처": equity_source},
            {"데이터": f"{equity_label} 구성종목 가격", "출처": price_source},
            {"데이터": "한국 금리" if korea_rates_active else "미국 금리", "출처": yield_source},
            {"데이터": fx_label, "출처": dxy_source},
            {"데이터": "실업률", "출처": unrate_source},
            {"데이터": "비농업 고용", "출처": payrolls_source},
            {"데이터": "CPI", "출처": cpi_source},
            {"데이터": "Core PCE", "출처": core_pce_source},
            {"데이터": "산업생산", "출처": indpro_source},
            {"데이터": "소매판매", "출처": retail_source},
            {"데이터": "주택착공", "출처": housing_source},
            {"데이터": "소비심리", "출처": sentiment_source},
            {"데이터": "M2", "출처": m2_source},
            {"데이터": "기준금리", "출처": fedfunds_source},
            {"데이터": "실질 GDP", "출처": gdp_source},
        ]
    )
    market_series = pd.concat([spx.rename(equity_label), dxy.rename(fx_label)], axis=1).dropna(how="all")
    yield_curve_series = yields[yield_ids].dropna(how="all")
    rate_series = pd.concat([dgs2.rename("2Y"), dgs10.rename("10Y"), dgs30.rename("30Y"), curve_10y_3m, curve_10y_2y, curve_5y_2y, curve_30y_10y], axis=1).dropna(how="all")
    risk_series = pd.concat([spx.rename(equity_label), drawdown, rolling_vol], axis=1).dropna(how="all")
    stress_series = pd.concat([rolling_vol, drawdown, fx_vol, curve_10y_2y], axis=1).dropna(how="all")

    return MacroDashboard(
        as_of_date=as_of,
        regime_label=regime_label,
        risk_level=risk_level,
        equity_bias=equity_bias,
        summary=summary,
        scores=scores,
        indicators=indicators,
        fred_macro=fred_macro,
        macro_pulse=macro_pulse,
        regime_scenarios=regime_scenarios,
        rates=rates,
        rate_diagnostics=rate_diagnostics,
        risk_assets=risk_assets,
        risk_breadth=risk_breadth,
        risk_stress=risk_stress,
        dollar_commodities=dollar_commodities,
        dollar_sensitivity=dollar_sensitivity,
        sector_playbook=sector_bias,
        sector_attribution=sector_attribution,
        sources=sources,
        market_series=market_series,
        rate_series=rate_series,
        yield_curve_series=yield_curve_series,
        risk_series=risk_series,
        stress_series=stress_series,
        commodity_series=domestic_series,
    )
