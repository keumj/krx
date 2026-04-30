$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
python -m uvicorn app.main:app --host 0.0.0.0 --port 8515

