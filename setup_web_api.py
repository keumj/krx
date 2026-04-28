import os
import shutil
from pathlib import Path

# 1. 경로 설정
base_dir = Path(r"c:\keumjm-stack")
source_dir = base_dir / "Keumj"
target_dir = base_dir / "web_api"

# 2. 필요한 하위 폴더 구조 생성
folders = [
    target_dir / "app" / "api" / "v1",
    target_dir / "app" / "schemas",
    target_dir / "static",
]

print("--- web_api 구조 생성 시작 ---")
for folder in folders:
    folder.mkdir(parents=True, exist_ok=True)
    print(f"Created: {folder}")

# 3. 기존 Keumj에서 분석 엔진(파이프라인) 및 데이터 복사
pipelines = ["pipeline_stock", "pipeline_portfolio", "pipeline_stock_news", "pipeline_common", "data"]

for pipe in pipelines:
    src = source_dir / pipe
    dst = target_dir / pipe
    if src.exists():
        try:
            # dirs_exist_ok=True를 사용하여 서버 실행 중에도 잠기지 않은 파일은 갱신 가능하게 합니다.
            shutil.copytree(
                src, 
                dst, 
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns('tmp*', '__pycache__', '.ipynb_checkpoints', '*.lock', '*.tmp')
            )
            print(f"Updated: {pipe} -> web_api/{pipe}")
        except PermissionError:
            print(f"Warning: {pipe} 일부 파일이 사용 중이라 건너뛰었습니다.")
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
pykrx
torch
statsmodels
copulas
pillow
"""
(target_dir / "requirements.txt").write_text(req_content, encoding="utf-8")

# [4-2] app/main.py
main_py = """from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
import sys
import os
from pathlib import Path

# 실행 위치와 관계없이 web_api 루트를 시스템 경로에 등록
root_path = Path(__file__).resolve().parents[1]
if str(root_path) not in sys.path:
    sys.path.insert(0, str(root_path))

from app.api.v1.endpoints import router as api_router
import uvicorn

app = FastAPI(
    title="Keumj Stock Analysis API",
    description="S&P 500 분석 파이프라인의 독립형 API 서비스",
    version="1.0.0"
)

# 프론트엔드 연결을 위한 CORS 설정
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")

# 정적 파일 서빙 및 루트 리다이렉트 추가
app.mount("/static", StaticFiles(directory=str(root_path / "static")), name="static")

@app.get("/")
async def root():
    return RedirectResponse(url="/static/index.html")

if __name__ == "__main__":
    # 로컬에서 실행 시: python app/main.py
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
"""
(target_dir / "app" / "main.py").write_text(main_py, encoding="utf-8")

# [4-3] app/schemas/stock.py
schemas_py = """from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

class ForecastRequest(BaseModel):
    ticker: str = Field(..., example="AAPL")
    horizon: int = Field(10, description="예측 기간 (영업일)")
    years: int = Field(8, description="학습에 사용할 과거 데이터 연수")

class ForecastResponse(BaseModel):
    ticker: str
    as_of_date: str
    forecast_date: str
    last_close: float
    predicted_price: float
    expected_return_pct: float
    direction_signal: str
    chart_base64: str

class FinancialResponse(BaseModel):
    ticker: str
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

class PortfolioSummary(BaseModel):
    holding_count: int
    market_value: float
    total_return_pct: Optional[float]
    benchmark_beta: Optional[float]

class DashboardResponse(BaseModel):
    summary: List[Dict[str, Any]]
    positions: List[Dict[str, Any]]
    cumulative_chart: Optional[str]
    sector_allocation_chart: Optional[str]

class TradeRequest(BaseModel):
    trade_date: str = Field(..., example="2024-04-21")
    ticker: str = Field(..., example="AAPL")
    side: str = Field(..., example="BUY")
    quantity: float
    price: float
"""
(target_dir / "app" / "schemas" / "stock.py").write_text(schemas_py, encoding="utf-8")

# [4-4] app/api/v1/endpoints.py
endpoints_py = """from fastapi import APIRouter, HTTPException
from typing import List, Dict, Any
import sys
import os
from pathlib import Path

# 1. 현재 파일의 위치를 기준으로 web_api 폴더 루트를 찾습니다.
# web_api/app/api/v1/endpoints.py -> parents[3]이 web_api 루트입니다.
current_file = Path(__file__).resolve()
web_api_root = current_file.parents[3]

# 2. PYTHONPATH에 web_api_root를 추가하여 복사된 모듈들을 최상위 패키지로 인식하게 합니다.
if str(web_api_root) not in sys.path:
    sys.path.insert(0, str(web_api_root))

# 3. 기존 모듈들이 web_api 내부의 data 폴더를 사용하도록 환경 변수를 강제 설정합니다.
os.environ["KEUMJ_PORTFOLIO_DB_DIR"] = str(web_api_root / "data" / "portfolio")
os.environ["SP500_COMPONENTS_CSV_PATH"] = str(web_api_root / "data" / "sp500_components_full.csv")

from pipeline_stock.web_gui import (
    _run_once, 
    _run_financial_once, 
    _run_risk_once, 
    _price_forecast_chart,
    _model_weight_chart
)
from pipeline_portfolio.analysis import build_portfolio_dashboard, add_trade, delete_trade
from app.schemas.stock import (
    ForecastRequest, ForecastResponse, FinancialResponse, RiskResponse,
    DashboardResponse, TradeRequest
)

router = APIRouter()

@router.post("/forecast", response_model=ForecastResponse)
async def get_forecast(req: ForecastRequest):
    \"\"\"주가 예측 실행 (앙상블 모델)\"\"\"
    try:
        form_data = {
            "ticker": req.ticker.upper(),
            "forecast_horizon": str(req.horizon),
            "history_years": str(req.years),
            "auto_save": "off"
        }
        # 기존 web_gui.py의 로직 실행
        ctx = _run_once(form_data)
        
        summary = ctx.result.summary.to_dict(orient="records")[0]
        
        return {
            "ticker": summary.get("ticker"),
            "as_of_date": summary.get("as_of_date"),
            "forecast_date": summary.get("forecast_date"),
            "last_close": summary.get("last_close"),
            "predicted_price": summary.get("predicted_price"),
            "expected_return_pct": summary.get("expected_return_pct"),
            "direction_signal": summary.get("direction_signal"),
            "chart_base64": _price_forecast_chart(ctx.result)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/financials/{ticker}", response_model=FinancialResponse)
async def get_financials(ticker: str, periods: int = 4):
    \"\"\"재무제표 및 밸류에이션 정보 조회\"\"\"
    try:
        form_data = {
            "ticker": ticker.upper(),
            "statement_periods": str(periods),
            "auto_save": "off",
            "fmp_api_key": os.getenv("FMP_API_KEY", "")
        }
        ctx = _run_financial_once(form_data)
        
        return {
            "ticker": ticker.upper(),
            "company_name": ctx.company_name,
            "currency": ctx.currency or "USD",
            "metrics": ctx.metrics,
            "summary": ctx.summary_table.to_dict(orient="records")
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/risk/{ticker}", response_model=RiskResponse)
async def get_risk_analysis(ticker: str):
    \"\"\"리스크 지표(변동성, MDD, VaR 등) 분석\"\"\"
    try:
        form_data = {"ticker": ticker.upper()}
        ctx = _run_risk_once(form_data)
        
        summary = ctx.summary_table.to_dict(orient="records")
        # 리스트 형태의 요약을 딕셔너리로 변환
        summary_dict = {item['Metric']: item['Ticker'] for item in summary}

        return {
            "ticker": ticker.upper(),
            "summary": summary_dict,
            "commentary": ctx.commentary,
            "drawdown_chart_base64": ctx.drawdown_chart_base64,
            "volatility_chart_base64": ctx.volatility_chart_base64
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/financials/{ticker}", response_model=FinancialResponse)
async def get_financials(ticker: str, periods: int = 4):
    try:
        form_data = {"ticker": ticker.upper(), "statement_periods": str(periods), "auto_save": "off"}
        ctx = _run_financial_once(form_data)
        return {
            "ticker": ticker.upper(),
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
        summary_dict = {item['Metric']: item['Ticker'] for item in ctx.summary_table.to_dict(orient="records")}
        return {
            "ticker": ticker.upper(),
            "summary": summary_dict,
            "commentary": ctx.commentary,
            "drawdown_chart_base64": ctx.drawdown_chart_base64,
            "volatility_chart_base64": ctx.volatility_chart_base64
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/portfolio", response_model=DashboardResponse)
async def get_portfolio_dashboard():
    \"\"\"현재 포트폴리오 상태 및 성과 분석\"\"\"
    try:
        # 내부 data 폴더의 DB를 참조함
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

@router.post("/portfolio/trade")
async def post_trade(req: TradeRequest):
    \"\"\"새로운 거래 기록 추가\"\"\"
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
"""
(target_dir / "app" / "api" / "v1" / "endpoints.py").write_text(endpoints_py, encoding="utf-8")

print("--- 구축 완료 ---")
print(f"이제 '{target_dir}' 폴더에서 서버를 실행할 수 있습니다.")