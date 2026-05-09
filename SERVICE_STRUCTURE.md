# Keumjm Single-Port Service

This service keeps the existing pipeline packages intact and adds a FastAPI
orchestration layer under `app/`.

## Run

```powershell
.\run_service.ps1
```

If PowerShell blocks `.ps1` scripts because of the execution policy, use:

```cmd
run_service.cmd
```

If Python or dependencies are not ready yet, run:

```cmd
setup_service.cmd
run_service.cmd
```

Equivalent command:

```powershell
python -m uvicorn app.main:app --host 0.0.0.0 --port 8515
```

## Main Routes

- `/` service landing page
- `/portfolio/overview?intent=run` portfolio dashboard
- `/portfolio/data-entry` trade entry
- `/portfolio/attribution` attribution
- `/portfolio/risk` risk
- `/portfolio/scoring` integrated scoring
- `/portfolio/optimization?intent=run` optimization
- `/stock/forecast` stock forecast
- `/stock/financials` stock financials and valuation
- `/stock/technical` technical analysis
- `/stock/returns` relative returns
- `/stock/risk` stock risk
- `/stock/factor-regime` factor and regime analysis
- `/stock/decision` decision dashboard
- `/stock/walk-forward` walk-forward validation
- `/stock-news/overview` stock-news overview
- `/stock-news/event-study` event study
- `/stock-news/sector-spillover` sector spillover
- `/stock-news/divergence` news-price divergence
- `/stock-news/expectation-reset` expectation reset
- `/stock-news/volatility-regime` volatility regime
- `/stock-news/topic-modeling` topic modeling
- `/refresh` refresh jobs
- `/docs` generated FastAPI API docs

## API Routes

- `GET /healthz`
- `GET /api/portfolio/dashboard`
- `GET /api/refresh/jobs`
- `POST /api/refresh/jobs/{job_id}/run`

## Ownership

- `pipeline_krx_portfolio`, `pipeline_krx_stock`, `pipeline_krx_stock_news`,
  `pipeline_krx_macro`, and `pipeline_common` remain the analytics/runtime modules.
- `app/services/*` are thin wrappers that hold web state, call pipeline
  functions, and adapt existing HTML to the new URL structure.
- `app/routers/*` define the web and API routes for the single-port service.
