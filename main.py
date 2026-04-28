import os
import shutil
from pathlib import Path

# 1. 경로 설정
base_dir = Path(r"c:\keumjm-stack")
source_dir = base_dir / "Keumj"
target_dir = base_dir / "web_api"

# 2. 필요한 하위 폴더 구조 생성
folders = [
    target_dir / "app" / "api",
    target_dir / "app" / "api" / "v1",
    target_dir / "app" / "schemas",
    target_dir / "app" / "static",
]

print("--- web_api 구조 생성 시작 ---")
for folder in folders:
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / "__init__.py").exists(): # 이미 존재하면 생성하지 않음
        (folder / "__init__.py").touch(exist_ok=True)
    print(f"Created: {folder}")

# app 폴더 루트에도 __init__.py 생성 (이미 존재하면 생성하지 않음)
if not (target_dir / "app" / "__init__.py").exists():
    (target_dir / "app" / "__init__.py").touch(exist_ok=True)

# 3. 기존 Keumj에서 분석 엔진(파이프라인) 및 데이터 복사
pipelines = ["pipeline_stock", "pipeline_portfolio", "pipeline_stock_news", "pipeline_common", "data"]

for pipe in pipelines:
    src = source_dir / pipe
    dst = target_dir / pipe
    if src.exists():
        try:
            # shutil.rmtree 대신 dirs_exist_ok=True를 사용하여 
            # 사용 중이지 않은 파일들은 최대한 업데이트하고, 잠긴 파일은 건너뛰거나 에러를 알립니다.
            shutil.copytree(
                src, 
                dst, 
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns('tmp*', '__pycache__', '.ipynb_checkpoints', '*.lock', '*.tmp')
            )
            print(f"Updated: {pipe} -> web_api/{pipe}")
        except PermissionError:
            print(f"Warning: {pipe} 복사 중 일부 파일이 사용 중이라 건너뛰었습니다. (해당 파일을 닫고 재실행하세요)")
    else:
        print(f"Warning: {src} 폴더를 찾을 수 없습니다.")
# 4. FastAPI 핵심 파일 작성
# [4-1] requirements.txt
req_content = """fastapi==0.110.0
uvicorn==0.27.0
pydantic==2.6.1
python-dotenv
pandas
numpy
matplotlib
scikit-learn
yfinance
requests
scipy
plotly
finance-datareader
torch
statsmodels
copulas
pillow
fredapi
nbformat
nbconvert
seaborn
"""
(target_dir / "requirements.txt").write_text(req_content, encoding="utf-8")

# [4-2] app/main.py
main_py = """import matplotlib
matplotlib.use('Agg')  # 최상단에서 GUI 백엔드 비활성화

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
import sys
import traceback
import webbrowser
import threading
from pathlib import Path

print("\\n[Step 1] Initializing Paths...")
current_file = Path(__file__).resolve()
web_api_root = current_file.parents[1]
print(f" - Web API Root: {web_api_root}")

if str(web_api_root) not in sys.path:
    sys.path.insert(0, str(web_api_root))

print("[Step 2] Loading API Router...")
try:
    from app.api.v1.endpoints import router as api_router
    print(" - API Router loaded successfully.")
except Exception as e:
    print("!!! Critical error during router loading !!!")
    traceback.print_exc()
    sys.exit(1)

import uvicorn

app = FastAPI(
    title="Keumj Stock Analysis API",
    description="S&P 500 분석 파이프라인의 독립형 API 서비스",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")

# 정적 파일 경로를 절대 경로로 확정 (app/static)
static_path = (web_api_root / "app" / "static").resolve()
print(f"[Step 3] Mounting Static Files: {static_path}")
app.mount("/static", StaticFiles(directory=str(static_path), html=True), name="static")

@app.get("/")
async def root():
    return RedirectResponse(url="/static/index.html")

def open_browser():
    try:
        print(" - Opening browser to http://localhost:8000...")
        webbrowser.open("http://localhost:8000")
    except Exception:
        pass

if __name__ == "__main__":
    print("\\n--- Keumj API Server Starting ---")
    print(f"URL: http://localhost:8000")
    if not (static_path / 'index.html').exists():
        print(f"WARNING: index.html not found in {static_path}")
    
    # 서버 실행 후 1.5초 뒤에 브라우저 오픈
    threading.Timer(1.5, open_browser).start()

    print("Starting Uvicorn (Hanging check: if this is the last line, imports are okay)...")
    uvicorn.run(app, host="127.0.0.1", port=8000)
"""
(target_dir / "app" / "main.py").write_text(main_py, encoding="utf-8")

# [4-3] app/schemas/stock.py
schemas_py = """from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

class ForecastRequest(BaseModel):
    ticker: str = Field(..., example="AAPL")
    horizon: Optional[int] = Field(10)
    years: Optional[int] = Field(8)

class ForecastResponse(BaseModel):
    ticker: Optional[str] = None
    as_of_date: Optional[str] = None
    forecast_date: Optional[str] = None
    horizon_days: Optional[int] = None
    last_close: Optional[float] = None
    predicted_price: Optional[float] = None
    expected_return_pct: Optional[float] = None
    direction_prob_up_pct: Optional[float] = None
    direction_confidence_pct: Optional[float] = None
    direction_signal: Optional[str] = None
    trade_filter: Optional[str] = None
    chart_base64: Optional[str] = None

class FinancialResponse(BaseModel):
    ticker: Optional[str] = None
    company_name: str
    currency: str
    metrics: Dict[str, Any]
    summary: List[Dict[str, Any]]

class RiskResponse(BaseModel):
    ticker: str
    summary: Dict[str, Any]
    commentary: str
    drawdown_chart_base64: str
    volatility_chart_base64: str

class DashboardResponse(BaseModel):
    summary: List[Dict[str, Any]]
    positions: List[Dict[str, Any]]
    cumulative_chart: Optional[str]
    sector_allocation_chart: Optional[str]

class TradeRequest(BaseModel):
    trade_date: str = Field(..., example="2025-12-31")
    ticker: str = Field(..., example="AAPL")
    side: str = Field(..., example="BUY")
    quantity: float
    price: float
"""
(target_dir / "app" / "schemas" / "stock.py").write_text(schemas_py, encoding="utf-8")

# [4-4] static/index.html (가시적인 대시보드 추가)
index_html = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Keumj Lab | S&P 500 Portfolio & Analysis</title>
    <style>
        :root {
            --bg: #f3f5f7;
            --card: #ffffff;
            --line: #d4dde8;
            --text: #1f2937;
            --muted: #5f6b7a;
            --brand: #111111;
            --accent: #0f4c81;
            --ok-bg: #e8f7ee;
            --ok-line: #99d5af;
            --err-bg: #fff2f2;
            --err-line: #efadad;
        }
        * { box-sizing: border-box; }
        body { margin: 0; background: var(--bg); color: var(--text); font-family: "Segoe UI", "Noto Sans KR", "Malgun Gothic", sans-serif; -webkit-font-smoothing: antialiased; }
        .wrap { max-width: 1460px; margin: 0 auto; padding: 20px; }
        
        .page-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; margin-bottom: 5px; }
        .page-head h1 { margin: 0; font-size: 24px; font-weight: 800; color: var(--brand); letter-spacing: -0.5px; }
        .page-credit { color: var(--muted); font-size: 11px; white-space: nowrap; padding-top: 6px; }
        .sub { color: var(--muted); margin-bottom: 18px; font-size: 14px; line-height: 1.5; }

        .nav { display: flex; gap: 8px; margin-bottom: 15px; flex-wrap: wrap; }
        .nav button { border: 1px solid #111; background: #fff; border-radius: 999px; padding: 8px 16px; font-size: 13px; cursor: pointer; font-weight: 600; color: #111; transition: all 0.2s; }
        .nav button.active { background: #111; color: #fff; }
        .nav button:hover:not(.active) { background: #eee; }

        .card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 16px; margin-bottom: 15px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
        .card h3 { margin-top: 0; font-size: 16px; color: var(--brand); margin-bottom: 12px; font-weight: 700; border-bottom: 1px solid #f0f0f0; padding-bottom: 8px; }

        .toolbar { display: flex; gap: 12px; align-items: flex-end; flex-wrap: wrap; margin-bottom: 5px; }
        .field { display: flex; flex-direction: column; gap: 4px; }
        .field label { font-size: 11px; color: var(--muted); font-weight: 700; text-transform: uppercase; }
        .field input { padding: 9px 12px; border: 1px solid var(--line); border-radius: 6px; width: 220px; font-size: 14px; }
        
        .btn-main { background: #111; border: 0; color: #fff; padding: 10px 20px; border-radius: 6px; cursor: pointer; font-weight: 700; font-size: 13px; }
        .btn-main:hover { background: #333; }
        
        .metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-top: 15px; margin-bottom: 15px; }
        .metric { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 14px; border-left: 5px solid var(--accent); }
        .metric span { display: block; font-size: 11px; color: var(--muted); font-weight: 700; text-transform: uppercase; margin-bottom: 4px; }
        .metric strong { display: block; font-size: 19px; line-height: 1.2; color: var(--brand); font-weight: 800; }

        .result-section { display: none; }
        .grid-2 { display: grid; grid-template-columns: repeat(2, minmax(320px, 1fr)); gap: 12px; }
        
        .table-wrap { width: 100%; overflow-x: auto; margin-top: 5px; }
        .data-table { width: 100%; border-collapse: collapse; font-size: 12px; background: #fff; }
        .data-table th, .data-table td { border: 1px solid var(--line); padding: 9px 10px; text-align: left; vertical-align: middle; }
        .data-table th { background: #f9fafb; color: var(--muted); font-weight: 700; text-transform: uppercase; font-size: 11px; }
        .data-table tr:hover { background: #fcfcfc; }
        
        .chart-img { width: 100%; height: auto; border-radius: 6px; border: 1px solid var(--line); display: block; }
        
        #loading { display: none; margin-bottom: 15px; padding: 14px; background: var(--ok-bg); border: 1px solid var(--ok-line); border-radius: 8px; color: var(--brand); font-weight: 700; text-align: center; }
        .notice { margin-top: 10px; border-radius: 8px; padding: 10px; font-size: 13px; }
        .sub-text { color: var(--muted); font-size: 13px; line-height: 1.6; }
    </style>
</head>
<body>
    <div class="wrap">
        <div class="page-head">
            <h1>Portfolio Lab | S&P 500</h1>
            <div class="page-credit">Keumj 제작</div>
        </div>

        <div class="card">
            <div class="nav">
                <button id="nav-portfolio" class="active" onclick="switchTab('portfolio')">포트폴리오 개요</button>
                <button id="nav-forecast" onclick="switchTab('forecast')">앙상블 주가 예측</button>
                <button id="nav-financials" onclick="switchTab('financials')">재무제표 & 밸류에이션</button>
                <button id="nav-risk" onclick="switchTab('risk')">리스크 & 변동성</button>
            </div>
            <div class="sub" id="page-subtitle">보유 종목별 퍼포먼스와 포트폴리오 상태를 한눈에 봅니다.</div>
            
            <div class="toolbar">
                <div class="field">
                    <label>티커 검색 (Symbol)</label>
                    <input type="text" id="ticker" value="AAPL" placeholder="예: AAPL, MSFT, NVDA">
                </div>
                <button class="btn-main" onclick="executeCurrentTab()">분석 실행 (RUN)</button>
            </div>
        </div>

        <div id="loading">Keumj 분석 엔진 가동 중... 잠시만 기다려 주세요.</div>

        <div id="metrics-bar" class="metrics" style="display:none;"></div>

        <!-- 포트폴리오 개요 섹션 -->
        <div id="portfolio-result" class="result-section">
            <div id="portfolio-metrics" class="metrics"></div>
            <div class="card">
                <h3>Portfolio Overview</h3>
                <div class="table-wrap"><table id="portfolio-summary-table" class="data-table"></table></div>
            </div>
            <div class="grid-2">
                <div class="card">
                    <h3>Performance Trend</h3>
                    <img id="portfolio-chart" class="chart-img" src="">
                </div>
                <div class="card">
                    <h3>Active Positions</h3>
                    <div class="table-wrap"><table id="portfolio-positions-table" class="data-table"></table></div>
                </div>
            </div>
        </div>

        <!-- 주가 예측 섹션 -->
        <div id="forecast-result" class="result-section">
            <div class="grid-2">
                <div class="card">
                    <h3>10-Day Ensemble Forecast</h3>
                    <img id="forecast-chart" class="chart-img" src="">
                </div>
                <div class="card">
                    <h3>Analysis Statistics</h3>
                    <div class="table-wrap"><table id="forecast-table" class="data-table"></table></div>
                </div>
            </div>
        </div>

        <!-- 재무제표 섹션 -->
        <div id="financial-result" class="result-section">
            <div class="card">
                <h3>Financial Statements & Key Ratios</h3>
                <div class="table-wrap"><table id="financial-table" class="data-table"></table></div>
            </div>
        </div>

        <!-- 리스크 분석 섹션 -->
        <div id="risk-result" class="result-section">
            <div class="card">
                <h3>Risk & Volatility Dashboard</h3>
                <p id="risk-commentary" class="sub-text"></p>
                <div class="grid-2">
                    <img id="drawdown-chart" class="chart-img" src="">
                    <img id="volatility-chart" class="chart-img" src="">
                </div>
                <div class="table-wrap" style="margin-top:15px;"><table id="risk-table" class="data-table"></table></div>
            </div>
        </div>
    </div>

    <script>
        let currentTab = 'forecast';

        function switchTab(tab) {
            currentTab = tab;
            document.querySelectorAll('.nav button').forEach(b => b.classList.remove('active'));
            document.getElementById('nav-' + tab).classList.add('active');
            
            const subtitles = {
                'portfolio': '보유 종목별 퍼포먼스와 포트폴리오 상태를 한눈에 봅니다.',
                'forecast': '앙상블 학습 모델을 이용한 향후 10거래일 주가 예측 신호를 확인합니다.',
                'financials': '최근 4분기 재무제표와 주요 밸류에이션 지표를 조회합니다.',
                'risk': '변동성, MDD, VaR 등 포트폴리오 및 종목 리스크 지표를 분석합니다.'
            };
            document.getElementById('page-subtitle').innerText = subtitles[tab] || '';
            hideSections();
        }

        function executeCurrentTab() {
            if(currentTab === 'portfolio') getPortfolio();
            else {
                const ticker = document.getElementById('ticker').value;
                if(!ticker) { alert('티커를 입력하세요.'); return; }
                if(currentTab === 'forecast') runForecast();
                else if(currentTab === 'financials') getFinancials();
                else if(currentTab === 'risk') getRisk();
            }
        }

        function renderMetrics(data) {
            const bar = document.getElementById('metrics-bar');
            bar.style.display = 'grid';
            let htmlContent = '';
            const items = [
                ['Ticker', data.ticker],
                ['Forecast Date', data.forecast_date],
                ['Predicted Price', '$' + data.predicted_price?.toLocaleString(undefined, {minimumFractionDigits: 2})],
                ['Exp. Return', data.expected_return_pct?.toFixed(2) + '%'],
                ['Signal', data.direction_signal],
                ['Confidence', data.direction_confidence_pct?.toFixed(1) + '%']
            ];
            items.forEach(i => {
                htmlContent += `<div class="metric"><span>${i[0]}</span><strong>${i[1] || '-'}</strong></div>`;
            });
            bar.innerHTML = htmlContent;
        }

        async function runForecast() {
            const ticker = document.getElementById('ticker').value;
            showLoading(true);
            hideSections();
            try {
                const res = await fetch('/api/v1/forecast', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ticker: ticker, horizon: 10, years: 8 })
                });
                const data = await res.json();
                if (res.ok) {
                    document.getElementById('forecast-result').style.display = 'block';
                    document.getElementById('forecast-chart').src = 'data:image/png;base64,' + data.chart_base64;
                    renderMetrics(data);
                    let tableHtml = '<thead><tr><th>항목</th><th>값</th></tr></thead><tbody>';
                    const fields = ['ticker', 'as_of_date', 'forecast_date', 'last_close', 'predicted_price', 'expected_return_pct', 'direction_signal', 'trade_filter'];
                    fields.forEach(f => {
                        tableHtml += `<tr><td>${f.replace(/_/g, ' ').toUpperCase()}</td><td>${data[f] || '-'}</td></tr>`;
                    });
                    tableHtml += '</tbody>';
                    document.getElementById('forecast-table').innerHTML = tableHtml;
                } else { alert('에러: ' + data.detail); }
            } catch (e) { console.error(e); }
            showLoading(false);
        }

        async function getFinancials() {
            const ticker = document.getElementById('ticker').value;
            showLoading(true);
            hideSections(); // Hide all sections first
            try { 
                const res = await fetch(`/api/v1/financials/${ticker}`);
                const data = await res.json();
                if (res.ok) {
                    document.getElementById('financial-result').style.display = 'block';
                    let tableHtml = `<thead><tr><th>Key Indicator</th><th>Value</th></tr></thead><tbody>`;
                    tableHtml += `<tr><td>COMPANY</td><td><strong>${data.company_name}</strong></td></tr>`;
                    for (const [k, v] of Object.entries(data.metrics)) {
                        tableHtml += `<tr><td>${k}</td><td>${typeof v === 'number' ? v.toLocaleString(undefined, {maximumFractionDigits: 4}) : v}</td></tr>`;
                    }
                    tableHtml += '</tbody>';
                    document.getElementById('financial-table').innerHTML = tableHtml;
                } else { alert('에러: ' + data.detail); }
            } catch (e) { console.error(e); }
            showLoading(false);
        }

        async function getRisk() {
            const ticker = document.getElementById('ticker').value;
            showLoading(true);
            hideSections(); // Hide all sections first
            try { 
                const res = await fetch(`/api/v1/risk/${ticker}`);
                const data = await res.json();
                if (res.ok) {
                    document.getElementById('risk-result').style.display = 'block';
                    document.getElementById('risk-commentary').innerText = data.commentary;
                    document.getElementById('drawdown-chart').src = 'data:image/png;base64,' + data.drawdown_chart_base64;
                    document.getElementById('volatility-chart').src = 'data:image/png;base64,' + data.volatility_chart_base64;
                    
                    let tableHtml = '<thead><tr><th>지표</th><th>값</th></tr></thead><tbody>';
                    for (const [k, v] of Object.entries(data.summary)) {
                        tableHtml += `<tr><td>${k}</td><td>${v}</td></tr>`;
                    }
                    tableHtml += '</tbody>';
                    document.getElementById('risk-table').innerHTML = tableHtml;
                } else { alert('에러: ' + data.detail); }
            } catch (e) { console.error(e); }
            showLoading(false);
        }

        async function getPortfolio() {
            showLoading(true);
            hideSections();
            try {
                const res = await fetch('/api/v1/portfolio');
                const data = await res.json();
                if (res.ok) {
                    document.getElementById('portfolio-result').style.display = 'block';
                    if(data.cumulative_chart) document.getElementById('portfolio-chart').src = data.cumulative_chart;
                    
                    // Metrics Grid (Original Style)
                    if (data.summary && data.summary.length > 0) {
                        const s = data.summary[0];
                        const mItems = [
                            ['Holdings', s.holding_count],
                            ['Market Value', '$' + s.market_value?.toLocaleString()],
                            ['Total Return', (s.total_return_pct?.toFixed(2) || '0.00') + '%'],
                            ['Beta (SPY)', s.benchmark_beta?.toFixed(3) || '-']
                        ];
                        let mHtml = '';
                        mItems.forEach(i => {
                            mHtml += `<div class="metric"><span>${i[0]}</span><strong>${i[1]}</strong></div>`;
                        });
                        document.getElementById('portfolio-metrics').innerHTML = mHtml;
                        
                        // Summary Table
                        let summaryTableHtml = '<thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>';
                        Object.keys(s).forEach(k => {
                            let val = s[k];
                            if(typeof val === 'number') {
                                if(k.includes('pct')) val = val.toFixed(2) + '%';
                                else val = val.toLocaleString();
                            }
                            summaryTableHtml += `<tr><td>${k.replace(/_/g, ' ').toUpperCase()}</td><td>${val}</td></tr>`;
                        });
                        summaryTableHtml += '</tbody>';
                        document.getElementById('portfolio-summary-table').innerHTML = summaryTableHtml;
                    }

                    // Positions Table
                    let posTableHtml = '<thead><tr><th>Ticker</th><th>Qty</th><th>Avg Price</th><th>Current</th><th>Value</th><th>PnL%</th></tr></thead><tbody>';
                    data.positions.forEach(pos => {
                        const pnlColor = pos.unrealized_pnl_pct >= 0 ? '#146c2e' : '#a12626';
                        posTableHtml += `<tr>
                            <td><strong>${pos.ticker}</strong></td>
                            <td>${pos.quantity.toLocaleString()}</td>
                            <td>$${pos.avg_price.toFixed(2)}</td>
                            <td>$${pos.market_price.toFixed(2)}</td>
                            <td>$${pos.market_value.toLocaleString()}</td>
                            <td style="color:${pnlColor}; font-weight:bold;">${pos.unrealized_pnl_pct.toFixed(2)}%</td>
                        </tr>`;
                    });
                    posTableHtml += '</tbody>';
                    document.getElementById('portfolio-positions-table').innerHTML = posTableHtml;
                }
            } catch (e) { console.error(e); }
            showLoading(false);
        }

        // 초기 실행
        window.onload = () => {
            getPortfolio();
        };

        function showLoading(show) { document.getElementById('loading').style.display = show ? 'block' : 'none'; }
        function hideSections() { 
            document.getElementById('forecast-result').style.display = 'none'; 
            document.getElementById('financial-result').style.display = 'none';
            document.getElementById('risk-result').style.display = 'none';
            document.getElementById('portfolio-result').style.display = 'none'; 
            document.getElementById('portfolio-metrics').innerHTML = '';
            document.getElementById('metrics-bar').style.display = 'none';
        }
    </script>
</body>
</html>
"""
(target_dir / "app" / "static" / "index.html").write_text(index_html, encoding="utf-8")

# [4-5] app/api/v1/endpoints.py (기존 파이프라인 연결 코드 전체)
endpoints_py = """from fastapi import APIRouter, HTTPException
from typing import List, Dict, Any
import sys
import os
import traceback
from pathlib import Path

# Matplotlib이 GUI 창을 띄우려다 멈추는 것을 방지 (FastAPI 앱에서는 필요 없을 수 있지만, 안전을 위해 유지)
import matplotlib
matplotlib.use('Agg')

router = APIRouter()

@router.get("/ping")
async def ping():
    \"\"\"서버 활성화 확인을 위한 헬스체크\"\"\"
    return {"status": "ok", "message": "Server is alive"}

print("   [Endpoints] Setting up environment...")
current_file = Path(__file__).resolve()
web_api_root = current_file.parents[3]

if str(web_api_root) not in sys.path:
    sys.path.insert(0, str(web_api_root))

os.environ["KEUMJ_PORTFOLIO_DB_DIR"] = str((web_api_root / "data" / "portfolio").resolve())
os.environ["SP500_COMPONENTS_CSV_PATH"] = str((web_api_root / "data" / "sp500_components_full.csv").resolve())
os.environ["KEUMJ_SHARED_SP500_DB_PATH"] = str((web_api_root / "data" / "sp500_shared_db" / "sp500_shared_prices.sqlite").resolve())

try:
    print("   [Endpoints] Importing pipeline_stock...")
    from pipeline_stock.web_gui import (
        _run_once, _run_financial_once, _run_risk_once, 
        _price_forecast_chart
    )
    print("   [Endpoints] Importing pipeline_portfolio...")
    from pipeline_portfolio.analysis import build_portfolio_dashboard, add_trade
except Exception:
    print("!!! Error importing analysis pipelines !!!")
    traceback.print_exc()
    raise

from app.schemas.stock import (
    ForecastRequest, ForecastResponse, FinancialResponse, RiskResponse, 
    DashboardResponse, TradeRequest
)

@router.post("/forecast", response_model=ForecastResponse)
async def get_forecast(req: ForecastRequest):
    try:
        form_data = {
            "ticker": req.ticker.upper(),
            "forecast_horizon": str(req.horizon),
            "history_years": str(req.years),
            "auto_save": "off"
        }
        ctx = _run_once(form_data)
        summary = ctx.result.summary.to_dict(orient="records")[0]
        
        return {
            "ticker": summary.get("ticker"),
            "as_of_date": summary.get("as_of_date"),
            "forecast_date": summary.get("forecast_date"),
            "horizon_days": int(summary.get("horizon_days", 0)),
            "last_close": summary.get("last_close"),
            "predicted_price": summary.get("predicted_price"),
            "expected_return_pct": summary.get("expected_return_pct"),
            "direction_prob_up_pct": summary.get("direction_prob_up_pct"),
            "direction_confidence_pct": summary.get("direction_confidence_pct"),
            "direction_signal": summary.get("direction_signal"),
            "trade_filter": summary.get("trade_filter"),
            "chart_base64": _price_forecast_chart(ctx.result)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/financials/{ticker}", response_model=FinancialResponse)
async def get_financials(ticker: str, periods: int = 4):
    try:
        form_data = {
            "ticker": ticker.upper(),
            "statement_periods": str(periods),
            "auto_save": "off"
        }
        ctx = _run_financial_once(form_data)
        return {
            "ticker": ctx.ticker.upper(), # Use ctx.ticker for consistency
            "company_name": ctx.company_name,
            "currency": ctx.currency or "USD",
            "metrics": ctx.metrics,
            "summary": ctx.summary_table.to_dict(orient="records")
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/risk/{ticker}", response_model=RiskResponse)
async def get_risk_analysis(ticker: str):
    try:
        form_data = {"ticker": ticker.upper()}
        ctx = _run_risk_once(form_data)
        # 리스트 형태의 요약을 딕셔너리로 변환하여 스키마 준수
        summary_dict = {item['Metric']: item['Ticker'] for item in ctx.summary_table.to_dict(orient="records")}
        
        return {
            "ticker": ctx.ticker.upper(), # Use ctx.ticker for consistency
            "summary": summary_dict,
            "commentary": ctx.commentary,
            "drawdown_chart_base64": ctx.drawdown_chart_base64,
            "volatility_chart_base64": ctx.volatility_chart_base64
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/portfolio/trade")
async def post_trade(req: TradeRequest):
    try:
        db_path = web_api_root / "data" / "portfolio" / "portfolio.sqlite"
        trade_id = add_trade(
            trade_date=req.trade_date,
            ticker=req.ticker.upper(),
            side=req.side.upper(),
            quantity=req.quantity,
            price=req.price,
            db_path=db_path
        )
        return {"status": "success", "trade_id": trade_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/portfolio", response_model=DashboardResponse)
async def get_portfolio_dashboard():
    try:
        db_path = web_api_root / "data" / "portfolio" / "portfolio.sqlite"
        shared_db = web_api_root / "data" / "sp500_shared_db" / "sp500_shared_prices.sqlite"
        
        ds = build_portfolio_dashboard(portfolio_db=db_path, shared_db=shared_db)
        
        return {
            "summary": ds.portfolio_summary.to_dict(orient="records"),
            "positions": ds.positions.to_dict(orient="records"),
            "cumulative_chart": f"data:image/png;base64,{ds.cumulative_chart}" if ds.cumulative_chart else None,
            "sector_allocation_chart": f"data:image/png;base64,{ds.sector_allocation_chart}" if ds.sector_allocation_chart else None
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
"""
(target_dir / "app" / "api" / "v1" / "endpoints.py").write_text(endpoints_py, encoding="utf-8")

print("--- 구축 완료 ---")
print(f"이제 '{target_dir}' 폴더에서 서버를 다시 실행하세요.")
print("브라우저에서 http://localhost:8000 접속 시 대시보드가 나타납니다.")
