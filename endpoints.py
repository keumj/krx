from fastapi import APIRouter, HTTPException
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
    """주가 예측 실행 (앙상블 모델)"""
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

@router.get("/portfolio", response_model=DashboardResponse)
async def get_portfolio_dashboard():
    """현재 포트폴리오 상태 및 성과 분석"""
    try:
        # web_api 내부의 data 경로를 직접 지정합니다.
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
    """새로운 거래 기록 추가"""
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

@router.get("/financials/{ticker}", response_model=FinancialResponse)
async def get_financials(ticker: str, periods: int = 4):
    """재무제표 및 밸류에이션 정보 조회"""
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
    """리스크 지표(변동성, MDD, VaR 등) 분석"""
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