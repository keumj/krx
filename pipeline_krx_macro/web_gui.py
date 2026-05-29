from __future__ import annotations

import base64
import html
import io
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from pipeline_common.notebook_models import select_quarter_snapshots
from pipeline_krx_stock.web_gui import _shared_theme_root_css

from .analysis import DEFAULT_LOOKBACK_DAYS, MacroDashboard, build_macro_dashboard


YIELD_CURVE_SERIES_IDS = ["DGS1MO", "DGS3MO", "DGS6MO", "DGS1", "DGS2", "DGS3", "DGS5", "DGS7", "DGS10", "DGS20", "DGS30"]
YIELD_CURVE_MATURITIES = np.array([0.08, 0.25, 0.5, 1, 2, 3, 5, 7, 10, 20, 30], dtype=float)
YIELD_CURVE_LABELS = ["1M", "3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y"]


PAGES: dict[str, tuple[str, str]] = {
    "overview": ("개요", "거시 국면, 핵심 점수, 주요 지표를 한 화면에서 봅니다."),
    "regime": ("레짐", "성장, 물가·비용, 정책, 위험선호 축으로 현재 시장 환경을 분류합니다."),
    "rates": ("금리/커브", "2년, 10년, 30년 금리와 장단기 스프레드를 점검합니다."),
    "risk": ("위험자산", "KOSPI200 수익률, 변동성, 낙폭을 기준으로 위험선호를 읽습니다."),
    "dollar": ("환율/국내지표", "원/달러 환율과 국내 비용·활동 지표를 함께 봅니다."),
    "credit-liquidity": ("신용/유동성", "100대지표의 여수신, 예금·대출, 통화량을 묶어 국내 신용 여건을 봅니다."),
    "real-economy": ("실물경기", "생산, 소비, 투자, 경기순환, 물가 지표로 이익 사이클의 방향을 점검합니다."),
    "external": ("대외/수출입", "환율, 국제수지, 수출입, 대외채권·채무로 원화와 외국인 수급 배경을 봅니다."),
    "household-labor": ("가계/고용", "소득, 분배, 고용, 노동, 인구 지표로 내수 기반과 비용 압력을 봅니다."),
    "real-estate": ("부동산", "주택가격, 전세가격, 지가, 건설 관련 지표로 금융·건설 리스크를 봅니다."),
    "corporate": ("기업체력", "기업경영지표로 매출, 이익률, 레버리지 체력을 점검합니다."),
    "playbook": ("섹터 플레이북", "현재 거시 환경에서 업종별 민감도와 선호도를 정리합니다."),
}


def normalize_page(page: str | None) -> str:
    key = str(page or "overview").strip().lower()
    return key if key in PAGES else "overview"


def _fmt(value: object, ndigits: int = 2) -> str:
    try:
        if pd.isna(value):
            return "-"
    except Exception:
        pass
    if isinstance(value, (int, float, np.floating)):
        return f"{float(value):,.{ndigits}f}"
    return html.escape(str(value))


def _table(frame: pd.DataFrame, *, max_rows: int = 80, table_class: str = "") -> str:
    if frame is None or frame.empty:
        return "<p class='service-muted'>표시할 데이터가 없습니다.</p>"
    show = frame.head(max_rows).copy()
    for col in show.columns:
        if pd.api.types.is_numeric_dtype(show[col]):
            show[col] = show[col].map(lambda x: "-" if pd.isna(x) else f"{float(x):,.2f}")
    classes = " ".join(part for part in ["service-table", table_class.strip()] if part)
    return f"<div class='service-table-wrap'>{show.to_html(index=False, border=0, classes=classes)}</div>"


def _macro_nav(active: str) -> str:
    links = []
    for key, (label, _) in PAGES.items():
        cls = "active" if key == active else ""
        links.append(f'<a class="{cls}" href="/macro/{key}">{html.escape(label)}</a>')
    return '<div class="macro-nav">' + "".join(links) + "</div>"


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    clean = frame.apply(pd.to_numeric, errors="coerce").dropna(how="all")
    if clean.empty:
        return clean
    out = clean.copy()
    for col in out.columns:
        series = out[col].dropna()
        if series.empty or float(series.iloc[0]) == 0:
            continue
        out[col] = out[col] / float(series.iloc[0]) * 100.0
    return out


def _return_since_days(series: pd.Series, days: int = 60) -> float:
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


def _chart_to_base64(fig) -> str:
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _line_chart(frame: pd.DataFrame, title: str, *, ylabel: str = "", normalize: bool = False, tail: int = DEFAULT_LOOKBACK_DAYS) -> str:
    data = _normalize(frame) if normalize else frame.copy()
    data = data.tail(tail).apply(pd.to_numeric, errors="coerce").dropna(how="all")
    fig, ax = plt.subplots(figsize=(7.2, 3.3))
    if data.empty:
        ax.text(0.5, 0.5, "No data", ha="center", va="center")
    else:
        for col in data.columns:
            series = data[col].dropna()
            if not series.empty:
                ax.plot(series.index, series.values, linewidth=1.8, label=str(col))
        ax.legend(loc="best", fontsize=8, frameon=False)
    ax.set_title(title, fontsize=11, loc="left")
    ax.set_ylabel(ylabel)
    ax.grid(True, alpha=0.25)
    ax.tick_params(axis="x", labelrotation=20, labelsize=8)
    ax.tick_params(axis="y", labelsize=8)
    return _chart_to_base64(fig)


def _multi_panel_line_chart(frame: pd.DataFrame, title: str, *, tail: int = DEFAULT_LOOKBACK_DAYS) -> str:
    data = frame.tail(tail).apply(pd.to_numeric, errors="coerce").dropna(how="all")
    columns = [col for col in data.columns if not data[col].dropna().empty]
    nrows = max(len(columns), 1)
    fig, axes = plt.subplots(nrows=nrows, ncols=1, figsize=(7.2, max(2.2, 1.75 * nrows)), sharex=False)
    if nrows == 1:
        axes = [axes]
    if not columns:
        axes[0].text(0.5, 0.5, "No data", ha="center", va="center")
    else:
        for ax, col in zip(axes, columns):
            series = data[col].dropna()
            ax.plot(series.index, series.values, linewidth=1.7, label=str(col))
            ax.set_ylabel(str(col), fontsize=8)
            ax.grid(True, alpha=0.22)
            ax.tick_params(axis="x", labelrotation=18, labelsize=7)
            ax.tick_params(axis="y", labelsize=7)
    axes[0].set_title(title, fontsize=11, loc="left")
    return _chart_to_base64(fig)


def _dual_axis_line_chart(
    left_frame: pd.DataFrame,
    right_frame: pd.DataFrame,
    title: str,
    *,
    left_ylabel: str = "",
    right_ylabel: str = "",
    normalize_left: bool = False,
    normalize_right: bool = False,
    tail: int = DEFAULT_LOOKBACK_DAYS,
) -> str:
    left = _normalize(left_frame) if normalize_left else left_frame.copy()
    right = _normalize(right_frame) if normalize_right else right_frame.copy()
    left = left.tail(tail).apply(pd.to_numeric, errors="coerce").dropna(how="all")
    right = right.tail(tail).apply(pd.to_numeric, errors="coerce").dropna(how="all")
    if not left.empty and not right.empty:
        start = max(left.dropna(how="all").index.min(), right.dropna(how="all").index.min())
        end = min(left.dropna(how="all").index.max(), right.dropna(how="all").index.max())
        if start <= end:
            left = left[(left.index >= start) & (left.index <= end)]
            right = right[(right.index >= start) & (right.index <= end)]
    fig, ax_left = plt.subplots(figsize=(7.2, 3.3))
    ax_right = ax_left.twinx()
    if left.empty and right.empty:
        ax_left.text(0.5, 0.5, "No data", ha="center", va="center")
    else:
        left_lines = []
        right_lines = []
        for col in left.columns:
            series = left[col].dropna()
            if not series.empty:
                left_lines.extend(ax_left.plot(series.index, series.values, linewidth=1.9, label=str(col)))
        for col in right.columns:
            series = right[col].dropna()
            if not series.empty:
                right_lines.extend(ax_right.plot(series.index, series.values, linewidth=1.7, linestyle="--", label=str(col)))
        lines = left_lines + right_lines
        if lines:
            ax_left.legend(lines, [line.get_label() for line in lines], loc="best", fontsize=8, frameon=False)
    ax_left.set_title(title, fontsize=11, loc="left")
    ax_left.set_ylabel(left_ylabel)
    ax_right.set_ylabel(right_ylabel)
    ax_left.grid(True, alpha=0.25)
    ax_left.tick_params(axis="x", labelrotation=20, labelsize=8)
    ax_left.tick_params(axis="y", labelsize=8)
    ax_right.tick_params(axis="y", labelsize=8)
    return _chart_to_base64(fig)


def _bar_chart(labels: list[str], values: list[float], title: str, *, ylabel: str = "") -> str:
    fig, ax = plt.subplots(figsize=(7.2, 3.3))
    vals = [0.0 if not np.isfinite(v) else float(v) for v in values]
    colors = ["#0f766e" if v >= 0 else "#a12626" for v in vals]
    ax.bar(labels, vals, color=colors, alpha=0.88)
    ax.axhline(0, color="#111827", linewidth=0.8)
    ax.set_title(title, fontsize=11, loc="left")
    ax.set_ylabel(ylabel)
    ax.grid(True, axis="y", alpha=0.25)
    ax.tick_params(axis="x", labelrotation=15, labelsize=8)
    ax.tick_params(axis="y", labelsize=8)
    return _chart_to_base64(fig)


def _horizontal_bar_chart(labels: list[str], values: list[float], title: str, *, xlabel: str = "") -> str:
    fig, ax = plt.subplots(figsize=(7.2, 3.3))
    vals = [0.0 if not np.isfinite(v) else float(v) for v in values]
    y_pos = np.arange(len(labels))
    colors = ["#0f766e" if v >= 0 else "#a12626" for v in vals]
    ax.barh(y_pos, vals, color=colors, alpha=0.88)
    ax.axvline(0, color="#111827", linewidth=0.8)
    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels, fontsize=8)
    ax.invert_yaxis()
    ax.set_title(title, fontsize=11, loc="left")
    ax.set_xlabel(xlabel)
    ax.grid(True, axis="x", alpha=0.25)
    ax.tick_params(axis="x", labelsize=8)
    return _chart_to_base64(fig)


def _stacked_horizontal_bar_chart(frame: pd.DataFrame, label_col: str, value_cols: list[str], title: str, *, xlabel: str = "") -> str:
    data = frame[[label_col, *value_cols]].copy()
    for col in value_cols:
        data[col] = pd.to_numeric(data[col], errors="coerce").fillna(0.0)
    fig, ax = plt.subplots(figsize=(7.2, 3.8))
    y_pos = np.arange(len(data))
    left = np.zeros(len(data))
    colors = ["#2563eb", "#d97706", "#7c3aed", "#0f766e"]
    legend_labels = {
        "성장 기여": "Growth",
        "물가/비용 기여": "Inflation",
        "정책/금리 기여": "Policy/Rates",
        "위험선호 기여": "Risk Appetite",
    }
    for idx, col in enumerate(value_cols):
        values = data[col].astype(float).values
        ax.barh(y_pos, values, left=left, label=legend_labels.get(col, col), color=colors[idx % len(colors)], alpha=0.86)
        left += values
    ax.set_yticks(y_pos)
    ax.set_yticklabels(data[label_col].astype(str).tolist(), fontsize=8)
    ax.invert_yaxis()
    ax.set_title(title, fontsize=11, loc="left")
    ax.set_xlabel(xlabel)
    ax.grid(True, axis="x", alpha=0.25)
    ax.legend(loc="lower right", fontsize=7, frameon=False)
    ax.tick_params(axis="x", labelsize=8)
    return _chart_to_base64(fig)


def _key_stats_judgement_chart(frame: pd.DataFrame, title: str) -> str:
    if frame is None or frame.empty or "시장 판단" not in frame.columns:
        return _horizontal_bar_chart(["No data"], [0.0], title, xlabel="count")
    order = [("유리", "Favorable"), ("중립", "Neutral"), ("불리", "Unfavorable"), ("판단 보류", "Pending")]
    counts = frame["시장 판단"].astype(str).value_counts()
    pairs = [(display, float(counts.get(raw, 0))) for raw, display in order if int(counts.get(raw, 0)) > 0]
    labels = [display for display, _ in pairs]
    values = [value for _, value in pairs]
    if not labels:
        labels, values = ["No data"], [0.0]
    return _horizontal_bar_chart(labels, values, title, xlabel="count")


def _yield_curve_chart(frame: pd.DataFrame, title: str) -> str:
    data = frame.reindex(columns=YIELD_CURVE_SERIES_IDS).apply(pd.to_numeric, errors="coerce").dropna(how="all")
    fig, ax = plt.subplots(figsize=(7.2, 3.3))
    clean = data.ffill().dropna()
    if clean.empty:
        ax.text(0.5, 0.5, "No data", ha="center", va="center")
    else:
        selected = select_quarter_snapshots(clean, n_quarters=5)
        for d in selected.index:
            values = selected.loc[d].values.astype(float)
            ax.plot(YIELD_CURVE_MATURITIES, values, marker="o", linewidth=1.7, markersize=3.5, label=d.strftime("%Y-%m-%d"))
        ax.legend(loc="best", fontsize=7, frameon=False, ncol=2)
    ax.set_title(title, fontsize=11, loc="left")
    ax.set_xlabel("Maturity")
    ax.set_ylabel("Yield (%)")
    ax.set_xticks(YIELD_CURVE_MATURITIES)
    ax.set_xticklabels(YIELD_CURVE_LABELS, rotation=0, fontsize=8)
    ax.grid(True, alpha=0.25)
    ax.tick_params(axis="y", labelsize=8)
    return _chart_to_base64(fig)


def _score_bars(scores: pd.DataFrame) -> str:
    rows: list[str] = []
    for _, row in scores.iterrows():
        label = html.escape(str(row.get("점수", "")))
        value = row.get("값")
        pct = 0.0 if pd.isna(value) else max(0.0, min(float(value), 100.0))
        rows.append(
            f"""
            <div class="macro-score-row">
              <div class="macro-score-label">{label}</div>
              <div class="macro-score-track"><span style="width:{pct:.1f}%"></span></div>
              <div class="macro-score-value">{pct:.1f}</div>
            </div>
            """
        )
    return "<div class='macro-score-grid'>" + "".join(rows) + "</div>"


def _hero(dashboard: MacroDashboard, description: str) -> str:
    return f"""
    <div class="macro-hero">
      <div>
        <h1>Macro Analysis | KRX</h1>
        <p>{html.escape(description)}</p>
      </div>
      <div class="macro-metrics">
        <div><span>리스크</span><strong>{html.escape(dashboard.risk_level)}</strong></div>
        <div><span>주식 의견</span><strong>{html.escape(dashboard.equity_bias)}</strong></div>
      </div>
    </div>
    """


def _chart_card(title: str, image: str, *, stacked: bool = False) -> str:
    extra_class = " macro-chart-card-stacked" if stacked else ""
    return f'<section class="service-card macro-chart-card{extra_class}"><h2>{html.escape(title)}</h2><img src="data:image/png;base64,{image}" alt="{html.escape(title)} chart" /></section>'


def _equity_column(dashboard: MacroDashboard) -> str:
    for column in ("KOSPI200", "KRX"):
        if column in dashboard.market_series.columns or column in dashboard.risk_series.columns:
            return column
    return str(dashboard.market_series.columns[0]) if len(dashboard.market_series.columns) else "KOSPI200"


def _columns(frame: pd.DataFrame, names: list[str]) -> list[str]:
    return [name for name in names if frame is not None and name in frame.columns and not frame[name].dropna().empty]


def _page_charts(page: str, dashboard: MacroDashboard) -> str:
    equity_col = _equity_column(dashboard)
    if page == "overview":
        domestic = dashboard.commodity_series
        fx_col = "USD/KRW" if "USD/KRW" in dashboard.market_series.columns else "DXY"
        overview_returns = {equity_col: dashboard.market_series[equity_col], fx_col: dashboard.market_series[fx_col]}
        if "Consumer Sentiment" in domestic.columns:
            overview_returns["Consumer Sentiment"] = domestic["Consumer Sentiment"]
        sentiment_frame = pd.DataFrame()
        if "Consumer Sentiment" in domestic.columns:
            monthly_equity = dashboard.market_series[equity_col].resample("MS").last().rename(equity_col)
            sentiment_frame = pd.concat([monthly_equity, domestic["Consumer Sentiment"]], axis=1).dropna(how="all")
        domestic_overview_cols = [col for col in ["CPI", "Consumer Sentiment", "Retail Sales"] if col in domestic.columns]
        charts = [
            (
                "KOSPI200과 환율 흐름",
                _dual_axis_line_chart(
                    dashboard.market_series[[equity_col]],
                    dashboard.market_series[[fx_col]],
                    f"{equity_col} and {fx_col}",
                    left_ylabel=equity_col,
                    right_ylabel=fx_col,
                ),
            ),
            (
                "KOSPI200과 소비자심리",
                _dual_axis_line_chart(
                    sentiment_frame[[equity_col]],
                    sentiment_frame[["Consumer Sentiment"]],
                    f"{equity_col} and Consumer Sentiment",
                    left_ylabel=equity_col,
                    right_ylabel="Consumer Sentiment",
                ) if {equity_col, "Consumer Sentiment"}.issubset(sentiment_frame.columns) else _line_chart(sentiment_frame, "Consumer Sentiment"),
            ),
            ("국내 핵심 지표", _multi_panel_line_chart(domestic[domestic_overview_cols], "Korea Core Indicators") if domestic_overview_cols else _line_chart(pd.DataFrame(), "Korea Core Indicators")),
            (
                "핵심 지표 최근 60일 변화율",
                _horizontal_bar_chart(
                    list(overview_returns.keys()),
                    [_return_since_days(series, 60) for series in overview_returns.values()],
                    "Key Indicator 60D Changes",
                    xlabel="%",
                ),
            ),
        ]
    elif page == "regime":
        scenarios = dashboard.regime_scenarios
        charts = [
            ("레짐 시나리오 근접도", _horizontal_bar_chart(scenarios["시나리오"].astype(str).tolist(), scenarios["근접도"].astype(float).tolist(), "Regime Scenario Proximity", xlabel="score")),
            (
                f"{equity_col}과 10Y-2Y 커브",
                _dual_axis_line_chart(
                    dashboard.market_series[[equity_col]],
                    dashboard.rate_series[["10Y-2Y"]],
                    "Equity Trend and Yield Curve",
                    left_ylabel=equity_col,
                    right_ylabel="10Y-2Y %p",
                ),
            ),
        ]
    elif page == "rates":
        charts = [
            ("한국 금리 커브", _yield_curve_chart(dashboard.yield_curve_series, "Korea Rate Curves (Quarter-end + Latest)")),
            ("만기간 스프레드", _line_chart(dashboard.rate_series[["10Y-3M", "10Y-2Y", "5Y-2Y", "30Y-10Y"]], "Maturity Spreads", ylabel="%p")),
        ]
    elif page == "risk":
        risk = dashboard.risk_series
        stress = dashboard.stress_series
        charts = [
            (
                f"{equity_col}과 낙폭",
                _dual_axis_line_chart(
                    risk[[equity_col]],
                    risk[["Drawdown"]],
                    f"{equity_col} and Drawdown",
                    left_ylabel=equity_col,
                    right_ylabel="drawdown %",
                ),
            ),
            ("20D 연율 변동성", _line_chart(risk[["20D Ann Vol"]], "Rolling 20D Annualized Volatility", ylabel="annual %")),
            (
                "한국형 스트레스 지표",
                _dual_axis_line_chart(
                    stress[["USD/KRW 20D Ann Vol"]],
                    stress[["10Y-2Y"]],
                    "FX Volatility and Korea Curve",
                    left_ylabel="USD/KRW vol, annual %",
                    right_ylabel="10Y-2Y %p",
                ) if {"USD/KRW 20D Ann Vol", "10Y-2Y"}.issubset(stress.columns) else _line_chart(stress, "FX Volatility and Korea Curve"),
            ),
        ]
    elif page == "dollar":
        domestic = dashboard.commodity_series
        fx_col = "USD/KRW" if "USD/KRW" in dashboard.market_series.columns else "DXY"
        columns = [col for col in domestic.columns if col != fx_col]
        returns_60d = [_return_since_days(domestic[c], 60) for c in columns]
        ecos = dashboard.ecos100_series
        fx_map = {
            "KR_USDKRW": "USD/KRW",
            "KR_JPYKRW": "JPY/KRW(100)",
            "KR_EURKRW": "EUR/KRW",
            "KR_CNYKRW": "CNY/KRW",
        }
        usdkrw = ecos["KR_USDKRW"] if "KR_USDKRW" in ecos.columns else dashboard.market_series.get("USD/KRW")
        eurkrw = ecos["KR_EURKRW"] if "KR_EURKRW" in ecos.columns else None
        jpykrw = ecos["KR_JPYKRW"] if "KR_JPYKRW" in ecos.columns else None
        cnykrw = ecos["KR_CNYKRW"] if "KR_CNYKRW" in ecos.columns else None
        major_left = pd.DataFrame({"USD/KRW": usdkrw}).dropna(how="all") if usdkrw is not None else pd.DataFrame()
        major_right = pd.DataFrame({"EUR/KRW": eurkrw}).dropna(how="all") if eurkrw is not None else pd.DataFrame()
        asia_left = pd.DataFrame({"JPY/KRW(100)": jpykrw}).dropna(how="all") if jpykrw is not None else pd.DataFrame()
        asia_right = pd.DataFrame({"CNY/KRW": cnykrw}).dropna(how="all") if cnykrw is not None else pd.DataFrame()
        combined_judgement = pd.concat(
            [
                dashboard.key_stats_credit_liquidity,
                dashboard.key_stats_real_economy,
                dashboard.key_stats_external,
                dashboard.key_stats_household_labor,
                dashboard.key_stats_real_estate,
                dashboard.key_stats_corporate,
            ],
            ignore_index=True,
        ).drop_duplicates(subset=["분류", "지표"], keep="first")
        charts = [
            *(
                [
                    (
                        "원/달러·원/유로 환율",
                        _dual_axis_line_chart(
                            major_left,
                            major_right,
                            "USD/KRW and EUR/KRW",
                            left_ylabel="USD/KRW",
                            right_ylabel="EUR/KRW",
                        ),
                    )
                ]
                if not major_left.empty or not major_right.empty
                else []
            ),
            *(
                [
                    (
                        "원/엔·원/위안 환율",
                        _dual_axis_line_chart(
                            asia_left,
                            asia_right,
                            "JPY/KRW(100) and CNY/KRW",
                            left_ylabel="JPY/KRW(100)",
                            right_ylabel="CNY/KRW",
                        ),
                    )
                ]
                if not asia_left.empty or not asia_right.empty
                else []
            ),
            ("국내 비용/활동 지표", _multi_panel_line_chart(domestic[columns], "Korea Macro Levels") if columns else _line_chart(pd.DataFrame(), "Korea Macro Levels")),
            ("국내 비용/활동 60D 변화율", _horizontal_bar_chart([str(c) for c in columns], returns_60d, "Korea Macro 60D Changes", xlabel="%")),
            ("종합 주식시장 판단 분포", _key_stats_judgement_chart(combined_judgement, "Combined Equity Market Judgement Mix")),
        ]
    elif page == "playbook":
        playbook = dashboard.sector_playbook
        attribution = dashboard.sector_attribution
        charts = [
            ("섹터 선호 점수", _horizontal_bar_chart(playbook["섹터"].astype(str).tolist(), playbook["선호 점수"].astype(float).tolist(), "Sector Preference Scores", xlabel="score")),
            (
                "섹터 점수 기여도",
                _stacked_horizontal_bar_chart(
                    attribution,
                    "섹터",
                    ["성장 기여", "물가/비용 기여", "정책/금리 기여", "위험선호 기여"],
                    "Sector Score Attribution",
                    xlabel="score",
                ),
            ),
        ]
    else:
        key_frame = {
            "credit-liquidity": dashboard.key_stats_credit_liquidity,
            "real-economy": dashboard.key_stats_real_economy,
            "external": dashboard.key_stats_external,
            "household-labor": dashboard.key_stats_household_labor,
            "real-estate": dashboard.key_stats_real_estate,
            "corporate": dashboard.key_stats_corporate,
        }.get(page, dashboard.key_stats_overview)
        charts = [("주식시장 판단 분포", _key_stats_judgement_chart(key_frame, "Equity Market Judgement Mix"))]
        core = dashboard.core_macro_series
        ecos = dashboard.ecos100_series
        if page == "credit-liquidity":
            liquidity_map = {"KR_DEPOSIT_RATE": "Deposit Rate", "KR_LOAN_RATE": "Loan Rate", "KR_M2": "M2"}
            liquidity_cols = _columns(ecos, list(liquidity_map))
            if liquidity_cols:
                charts.insert(0, ("여수신/유동성 추이", _multi_panel_line_chart(ecos[liquidity_cols].rename(columns=liquidity_map), "Deposit, Loan, Liquidity")))
            rate_map = {
                "KR_CALL_RATE": "Call",
                "KR_KORIBOR_3M": "KORIBOR 3M",
                "KR_CD_91D": "CD 91D",
                "KR_CORP_AA_3Y": "Corp AA- 3Y",
            }
            rate_cols = _columns(ecos, list(rate_map))
            if rate_cols:
                charts.insert(1, ("시장금리 추이", _line_chart(ecos[rate_cols].rename(columns=rate_map), "Korea Money and Credit Rates", ylabel="%")))
            spread_parts = {}
            if {"KR_LOAN_RATE", "KR_DEPOSIT_RATE"}.issubset(ecos.columns):
                spread_parts["Loan-Deposit"] = ecos["KR_LOAN_RATE"] - ecos["KR_DEPOSIT_RATE"]
            if {"KR_CORP_AA_3Y", "KR_TBOND_3Y"}.issubset(ecos.columns):
                spread_parts["Corp AA-3Y - Gov 3Y"] = ecos["KR_CORP_AA_3Y"] - ecos["KR_TBOND_3Y"]
            spread_frame = pd.DataFrame(spread_parts).dropna(how="all")
            if not spread_frame.empty:
                charts.insert(2, ("신용/조달 스프레드", _line_chart(spread_frame, "Credit and Funding Spreads", ylabel="%p")))
        elif page == "real-economy":
            activity_map = {
                "KR_ALL_INDUSTRY_PROD": "All Industry",
                "KR_IPI": "Manufacturing",
                "KR_SERVICE_PROD": "Services",
                "KR_RETAIL": "Retail",
                "KR_AUTO_RETAIL": "Auto Retail",
                "KR_FACILITY_INVEST": "Facility Inv.",
            }
            activity_cols = _columns(ecos, list(activity_map))
            if activity_cols:
                charts.insert(0, ("생산/소비/투자 추이", _multi_panel_line_chart(ecos[activity_cols].rename(columns=activity_map), "Production, Retail, Investment")))
            price_map = {"KR_CPI": "CPI", "KR_PPI": "PPI"}
            price_cols = _columns(ecos, list(price_map))
            if price_cols:
                charts.insert(1, ("물가 추이", _line_chart(ecos[price_cols].rename(columns=price_map), "Inflation Indexes")))
            cycle_map = {"KR_GDP": "Real GDP", "KR_CCSI": "Consumer Sentiment"}
            cycle_cols = _columns(ecos, list(cycle_map))
            if cycle_cols:
                charts.insert(2, ("성장/심리 추이", _multi_panel_line_chart(ecos[cycle_cols].rename(columns=cycle_map), "Growth and Sentiment")))
        elif page == "external":
            ecos = dashboard.ecos100_series
            balance_map = {
                "KR_CURRENT_ACCOUNT": "Current Account",
                "KR_DIRECT_INVEST_ASSET": "Direct Inv. Asset",
                "KR_DIRECT_INVEST_LIAB": "Direct Inv. Liab.",
                "KR_PORTFOLIO_INVEST_ASSET": "Portfolio Asset",
                "KR_PORTFOLIO_INVEST_LIAB": "Portfolio Liab.",
            }
            balance_cols = _columns(ecos, list(balance_map))
            if balance_cols:
                charts.insert(0, ("국제수지/투자수지", _multi_panel_line_chart(ecos[balance_cols].rename(columns=balance_map), "Balance of Payments and Investment Flows")))
            trade_map = {
                "KR_EXPORT_VALUE": "Exports",
                "KR_IMPORT_VALUE": "Imports",
                "KR_NET_TERMS_TRADE": "Net Terms",
                "KR_INCOME_TERMS_TRADE": "Income Terms",
            }
            trade_cols = _columns(ecos, list(trade_map))
            if trade_cols:
                charts.insert(1, ("수출입/교역조건", _multi_panel_line_chart(ecos[trade_cols].rename(columns=trade_map), "Trade and Terms of Trade")))
            position_map = {
                "KR_FX_RESERVES": "FX Reserves",
                "KR_EXTERNAL_DEBT": "External Debt",
                "KR_EXTERNAL_CLAIMS": "External Claims",
            }
            position_cols = _columns(ecos, list(position_map))
            if position_cols:
                charts.insert(2, ("대외 안정성", _multi_panel_line_chart(ecos[position_cols].rename(columns=position_map), "External Resilience")))
        elif page == "household-labor":
            labor_map = {
                "KR_UNRATE": "Unemployment/Legacy",
                "KR_EMP_RATE": "Employment Rate",
                "KR_ECON_ACTIVE_POP": "Economically Active",
                "KR_EMPLOYED_PERSONS": "Employed Persons",
            }
            labor_cols = _columns(ecos, list(labor_map))
            if labor_cols:
                charts.insert(0, ("고용 추이", _multi_panel_line_chart(ecos[labor_cols].rename(columns=labor_map), "Labor Market Indicators")))
            demand_map = {"KR_RETAIL": "Retail", "KR_CCSI": "Consumer Sentiment"}
            demand_cols = _columns(ecos, list(demand_map))
            if demand_cols:
                charts.insert(1, ("가계 수요 추이", _multi_panel_line_chart(ecos[demand_cols].rename(columns=demand_map), "Household Demand Indicators")))
            burden_map = {"KR_LOAN_RATE": "Loan Rate", "KR_CPI": "CPI"}
            burden_cols = _columns(ecos, list(burden_map))
            if burden_cols:
                charts.insert(2, ("가계 부담 지표", _multi_panel_line_chart(ecos[burden_cols].rename(columns=burden_map), "Household Cost Burden")))
        elif page == "real-estate":
            property_map = {
                "KR_HOUSE_PRICE": "House Price",
                "KR_RENT_PRICE": "Jeonse Price",
                "KR_LAND_PRICE_CHANGE": "Land Price MoM",
                "KR_CONSTRUCTION_COMPLETED": "Construction Completed",
                "KR_CONSTRUCTION_ORDERS": "Construction Orders",
                "KR_CONSTRUCTION_STARTS": "Construction Starts",
            }
            property_cols = _columns(ecos, list(property_map))
            if property_cols:
                charts.insert(0, ("부동산/건설 추이", _multi_panel_line_chart(ecos[property_cols].rename(columns=property_map), "Real Estate and Construction")))
            rate_map = {"KR_LOAN_RATE": "Loan Rate", "KR_CD_91D": "CD 91D"}
            rate_cols = _columns(ecos, list(rate_map))
            if rate_cols:
                charts.insert(1, ("부동산 금리 환경", _line_chart(ecos[rate_cols].rename(columns=rate_map), "Rate Backdrop for Real Estate", ylabel="%")))
            demand_map = {"KR_RETAIL": "Retail", "KR_CCSI": "Consumer Sentiment"}
            demand_cols = _columns(ecos, list(demand_map))
            if demand_cols:
                charts.insert(2, ("부동산 수요 배경", _multi_panel_line_chart(ecos[demand_cols].rename(columns=demand_map), "Demand Backdrop for Real Estate")))
        elif page == "corporate":
            cols = _columns(core, ["Industrial Production", "Retail Sales", "CPI"])
            if cols:
                charts.insert(0, ("매출/마진 매크로 배경", _multi_panel_line_chart(core[cols], "Corporate Sales and Cost Backdrop")))
            if equity_col in dashboard.market_series.columns:
                charts.insert(1, ("주식시장 추이", _line_chart(dashboard.market_series[[equity_col]], f"{equity_col} Trend")))
            cost_map = {"KR_PPI": "PPI", "KR_CORP_AA_3Y": "Corp AA-3Y"}
            cost_cols = _columns(ecos, list(cost_map))
            if cost_cols:
                charts.insert(2, ("기업 비용/조달 환경", _multi_panel_line_chart(ecos[cost_cols].rename(columns=cost_map), "Corporate Cost and Funding Backdrop")))
    return '<div class="macro-grid two macro-chart-grid">' + "".join(_chart_card(title, image) for title, image in charts) + "</div>"


def _overview_page(dashboard: MacroDashboard) -> str:
    return f"""
    {_hero(dashboard, PAGES["overview"][1])}
    {_macro_nav("overview")}
    {_page_charts("overview", dashboard)}
    <section class="service-card"><h2>크로스에셋 펄스</h2>{_table(dashboard.macro_pulse, table_class="macro-wide-table")}</section>
    <div class="macro-grid two">
      <section class="service-card"><h2>핵심 요약</h2>{_table(dashboard.summary)}</section>
      <section class="service-card"><h2>거시 점수</h2>{_score_bars(dashboard.scores)}</section>
    </div>
    <section class="service-card"><h2>주요 지표</h2>{_table(dashboard.indicators)}</section>
    <section class="service-card"><h2>ECOS 100대지표 핵심 체크</h2>{_table(dashboard.key_stats_overview, table_class="macro-wide-table")}</section>
    """


def _regime_page(dashboard: MacroDashboard) -> str:
    scores = dashboard.scores.set_index("점수")["값"]
    notes = pd.DataFrame(
        [
            {"축": "성장", "읽는 법": "KOSPI200 단기 모멘텀, 소비자심리, 소매판매, 10Y-2Y 커브를 함께 봅니다. 주식과 수요 심리가 같이 좋아지고 커브가 덜 눌리면 국내 성장 기대가 살아 있다는 뜻입니다.", "현재 점수": _fmt(scores.get("성장 모멘텀"))},
            {"축": "물가/비용", "읽는 법": "CPI, 원/달러 환율, 장기금리를 묶어 국내 비용 압력과 인플레 부담을 봅니다. 높을수록 기업 마진과 할인율에 부담이 생기므로 가격 전가력 있는 업종을 더 중시합니다.", "현재 점수": _fmt(scores.get("물가/비용 압력"))},
            {"축": "정책", "읽는 법": "2년 금리를 중심으로 시장이 예상하는 정책금리 부담을 읽습니다. 높은 점수는 금리 인하 기대가 약하거나 긴축 부담이 남아 있다는 뜻이라 장기 성장주 멀티플에는 불리합니다.", "현재 점수": _fmt(scores.get("정책 긴축도"))},
            {"축": "위험선호", "읽는 법": "수익률, 낙폭, 변동성을 조합해 시장이 위험을 받아들이는지 봅니다. 점수가 높으면 리스크 온, 낮으면 지수 베타보다 현금흐름과 방어력을 우선하는 구간으로 해석합니다.", "현재 점수": _fmt(scores.get("위험선호"))},
        ]
    )
    return f"""
    {_hero(dashboard, PAGES["regime"][1])}
    {_macro_nav("regime")}
    {_page_charts("regime", dashboard)}
    <section class="service-card"><h2>레짐 시나리오 근접도</h2>{_table(dashboard.regime_scenarios, table_class="macro-wide-table")}</section>
    <section class="service-card"><h2>한국 펀더멘털 체크</h2>{_table(dashboard.fred_macro, table_class="macro-wide-table")}</section>
    <section class="service-card"><h2>레짐 분해</h2>{_table(notes)}</section>
    <section class="service-card"><h2>점수 상세</h2>{_score_bars(dashboard.scores)}</section>
    """


def _rates_page(dashboard: MacroDashboard) -> str:
    return f"""
    {_hero(dashboard, PAGES["rates"][1])}
    {_macro_nav("rates")}
    {_page_charts("rates", dashboard)}
    <section class="service-card"><h2>커브 진단</h2>{_table(dashboard.rate_diagnostics, table_class="macro-wide-table")}</section>
    <section class="service-card"><h2>금리와 커브</h2>{_table(dashboard.rates, table_class="macro-rates-table")}</section>
    <section class="service-card"><h2>금리 100대지표</h2>{_table(dashboard.key_stats_rates, table_class="macro-wide-table")}</section>
    <section class="service-card"><h2>해석</h2><p class="service-muted">2년 금리는 시장이 예상하는 정책금리 경로, 10년 금리는 성장·물가·기간프리미엄이 섞인 장기 할인율, 10Y-2Y와 10Y-3M 스프레드는 경기 사이클의 압력을 읽는 지표입니다. 금리 레벨과 커브 방향을 같이 봐야 성장주 멀티플 부담인지, 경기 둔화 신호인지 구분할 수 있습니다.</p></section>
    """


def _risk_page(dashboard: MacroDashboard) -> str:
    return f"""
    {_hero(dashboard, PAGES["risk"][1])}
    {_macro_nav("risk")}
    {_page_charts("risk", dashboard)}
    <section class="service-card"><h2>한국형 시장 스트레스</h2>{_table(dashboard.risk_stress, table_class="macro-wide-table")}</section>
    <section class="service-card"><h2>시장 내부 폭</h2>{_table(dashboard.risk_breadth, table_class="macro-wide-table")}</section>
    <section class="service-card"><h2>위험자산 온도</h2>{_table(dashboard.risk_assets)}</section>
    """


def _dollar_page(dashboard: MacroDashboard) -> str:
    return f"""
    {_hero(dashboard, PAGES["dollar"][1])}
    {_macro_nav("dollar")}
    {_page_charts("dollar", dashboard)}
    <section class="service-card"><h2>환율 민감도</h2>{_table(dashboard.dollar_sensitivity, table_class="macro-wide-table")}</section>
    <section class="service-card"><h2>국내 비용/활동 지표</h2>{_table(dashboard.dollar_commodities)}</section>
    <section class="service-card"><h2>환율/대외 100대지표</h2>{_table(dashboard.key_stats_external, table_class="macro-wide-table")}</section>
    """


def _key_stats_page(dashboard: MacroDashboard, page: str, frame: pd.DataFrame) -> str:
    return f"""
    {_hero(dashboard, PAGES[page][1])}
    {_macro_nav(page)}
    {_page_charts(page, dashboard)}
    <section class="service-card"><h2>{html.escape(PAGES[page][0])} 100대지표</h2>{_table(frame, table_class="macro-wide-table")}</section>
    <section class="service-card"><h2>해석 기준</h2><p class="service-muted">이 표의 시장 판단은 최신 레벨을 주식시장 관점에서 빠르게 분류한 보조 의견입니다. 시계열 방향, 업종별 가격 전가력, 기업 이익 추정과 함께 확인할 때 의미가 커집니다.</p></section>
    """


def _playbook_page(dashboard: MacroDashboard) -> str:
    return f"""
    {_hero(dashboard, PAGES["playbook"][1])}
    {_macro_nav("playbook")}
    {_page_charts("playbook", dashboard)}
    <section class="service-card"><h2>섹터 점수 기여도</h2>{_table(dashboard.sector_attribution, table_class="macro-wide-table")}</section>
    <section class="service-card"><h2>섹터 플레이북</h2>{_table(dashboard.sector_playbook)}</section>
    <section class="service-card"><h2>사용 방법</h2><p class="service-muted">이 표는 매수/매도 신호가 아니라 현재 거시 환경에서 어떤 업종을 먼저 점검할지 정하는 우선순위입니다. 선호 점수가 높아도 해당 업종의 이익 추정, 밸류에이션, 내부 breadth가 나쁘면 보류하고, 점수가 낮아도 개별 기업의 방어력이나 특수 모멘텀이 있으면 예외로 다룹니다.</p></section>
    """


def render_body(
    page: str,
    *,
    start_date: str | None = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    dashboard: MacroDashboard | None = None,
) -> str:
    active = normalize_page(page)
    if dashboard is None:
        dashboard = build_macro_dashboard(start_date=start_date, lookback_days=lookback_days)
    page_html = {
        "overview": _overview_page,
        "regime": _regime_page,
        "rates": _rates_page,
        "risk": _risk_page,
        "dollar": _dollar_page,
        "credit-liquidity": lambda dash: _key_stats_page(dash, "credit-liquidity", dash.key_stats_credit_liquidity),
        "real-economy": lambda dash: _key_stats_page(dash, "real-economy", dash.key_stats_real_economy),
        "external": lambda dash: _key_stats_page(dash, "external", dash.key_stats_external),
        "household-labor": lambda dash: _key_stats_page(dash, "household-labor", dash.key_stats_household_labor),
        "real-estate": lambda dash: _key_stats_page(dash, "real-estate", dash.key_stats_real_estate),
        "corporate": lambda dash: _key_stats_page(dash, "corporate", dash.key_stats_corporate),
        "playbook": _playbook_page,
    }[active](dashboard)
    return f"""
    <style>      
      :root {{
        {_shared_theme_root_css()}
      }}
      body {{ background: var(--bg); color: var(--text); }}
      .service-main {{ color: var(--text); }}
      .service-brand {{ color: #111827; }}
      .service-nav a {{ color: #111827; border-color: var(--line); }}
      .service-nav a.active {{ background: #111827; color: #fff; border-color: #111827; }}
      .macro-nav {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px; }}
      .macro-nav a {{ text-decoration:none; color:#111827; border:1px solid var(--line); background:#fff; border-radius:999px; padding:7px 12px; font-size:13px; }}
      .macro-nav a.active {{ background:var(--brand); color:#fff; border-color:var(--brand); }}
      .service-main > .macro-nav:first-of-type {{ display:none; }}
      .macro-hero {{ display:flex; justify-content:space-between; gap:16px; align-items:flex-start; background:none; border:none; border-radius:8px; padding:18px 18px 8px; margin-bottom:4px; }}
      .macro-hero h1 {{ margin:4px 0 8px; font-size:26px; letter-spacing:0; }}
      .macro-hero p {{ margin:0; color:var(--muted); line-height:1.5; }}
      .macro-metrics {{ display:grid; grid-template-columns:repeat(2, minmax(120px, 1fr)); gap:8px; min-width:280px; }}
      .macro-metrics div {{ border:1px solid var(--line); border-radius:8px; padding:10px; background:#f8fafc; }}
      .macro-metrics span {{ display:block; color:var(--muted); font-size:12px; margin-bottom:4px; }}
      .macro-metrics strong {{ font-size:18px; }}
      .macro-grid {{ display:grid; gap:12px; margin-bottom:12px; }}
      .macro-grid.two {{ grid-template-columns:minmax(0, 1fr) minmax(0, 1fr); }}
      .service-card {{ margin-bottom:12px; }}
      .service-card h2 {{ margin:0 0 10px; font-size:18px; }}
      .macro-chart-card {{ overflow:hidden; }}
      .macro-chart-card img {{ display:block; width:100%; max-width:100%; height:auto; }}
      .macro-score-grid {{ display:grid; gap:9px; }}
      .macro-score-row {{ display:grid; grid-template-columns:150px 1fr 52px; gap:10px; align-items:center; }}
      .macro-score-label {{ font-size:13px; color:var(--text); }}
      .macro-score-track {{ height:10px; background:#e5e7eb; border-radius:999px; overflow:hidden; }}
      .macro-score-track span {{ display:block; height:100%; background:var(--accent); }}
      .macro-score-value {{ font-variant-numeric:tabular-nums; text-align:right; font-size:12px; color:var(--muted); }}
      .macro-wide-table th,
      .macro-wide-table td {{ white-space:normal; overflow-wrap:break-word; word-break:keep-all; }}
      .macro-wide-table th:last-child,
      .macro-wide-table td:last-child {{ min-width:18rem; max-width:34rem; }}
      .macro-rates-table th:last-child,
      .macro-rates-table td:last-child {{ min-width:22rem; max-width:32rem; white-space:normal; overflow-wrap:break-word; word-break:keep-all; }}
      @media (max-width:900px) {{
        .macro-hero {{ flex-direction:column; }}
        .macro-metrics {{ width:100%; min-width:0; }}
        .macro-grid.two {{ grid-template-columns:1fr; }}
        .macro-score-row {{ grid-template-columns:120px 1fr 44px; }}
      }}
    </style>
    {page_html}
    """


def _html_page(page: str, *, start_date: str | None = None, lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> str:
    body = render_body(page, start_date=start_date, lookback_days=lookback_days)
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Macro Analysis | KRX</title>
</head>
<body>
  <main class="service-main">
    {body}
  </main>
</body>
</html>
"""


def launch_web_gui(host: str = "localhost", port: int = 8526, open_browser: bool = False) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path.strip("/")
            page = path.split("/", 1)[0] if path else "overview"
            query = parse_qs(parsed.query)
            start_date = str(query.get("start_date", [""])[0]).strip() or None
            try:
                lookback_days = int(query.get("lookback_days", [str(DEFAULT_LOOKBACK_DAYS)])[0] or str(DEFAULT_LOOKBACK_DAYS))
            except ValueError:
                lookback_days = DEFAULT_LOOKBACK_DAYS
            payload = _html_page(page, start_date=start_date, lookback_days=lookback_days).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    server = ThreadingHTTPServer((host, int(port)), Handler)
    url = f"http://{host}:{port}"
    print(f"KRX Macro Analysis running at {url}", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="KRX macro analysis GUI")
    parser.add_argument("--web-gui", action="store_true", help="Accepted for shell compatibility")
    parser.add_argument("--host", default="localhost", help="Host for the KRX macro GUI")
    parser.add_argument("--port", type=int, default=8526, help="Port for the KRX macro GUI")
    parser.add_argument("--open-browser", action="store_true", help="Open browser automatically")
    args = parser.parse_args()
    launch_web_gui(host=str(args.host), port=int(args.port), open_browser=bool(args.open_browser))


run_web_gui = launch_web_gui
