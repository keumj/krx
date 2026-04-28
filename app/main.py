import matplotlib
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

print("\n[Step 1] Initializing Paths...")
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
    print("\n--- Keumj API Server Starting ---")
    print(f"URL: http://localhost:8000")
    if not (static_path / 'index.html').exists():
        print(f"WARNING: index.html not found in {static_path}")
    
    # 서버 실행 후 1.5초 뒤에 브라우저 오픈
    threading.Timer(1.5, open_browser).start()

    print("Starting Uvicorn (Hanging check: if this is the last line, imports are okay)...")
    uvicorn.run(app, host="127.0.0.1", port=8000)
