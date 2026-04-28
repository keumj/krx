from fastapi import APIRouter, HTTPException
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
    """서버 활성화 확인을 위한 헬스체크"""
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
