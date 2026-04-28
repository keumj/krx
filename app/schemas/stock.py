from pydantic import BaseModel, Field
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
