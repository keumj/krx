# Pipeline Stock

Unified stock workspace for stock forecast and technical analysis.

기본 데이터 경로:

- 종목 예측/기술분석은 기본적으로 공유 SQLite/로컬 캐시 데이터를 우선 사용합니다.
- `--prices-csv` 또는 GUI의 로컬 가격 CSV 입력은 기본 경로를 덮어쓰는 선택 입력입니다.

## Quick Start

Combined 3-page GUI:

```bat
run_pipeline_stock.bat
```

or:

```powershell
.\run_pipeline_stock.bat
```

## Portable Distribution

다른 Windows PC에서 Python 설치 없이 실행할 배포 폴더를 만들려면:

```bat
build_stock_distribution.bat
```

빌드 결과는 `dist\KeumjStockLab`에 생성됩니다. 이 폴더 전체를 복사한 뒤 대상 PC에서 `run_pipeline_stock.bat`을 실행하면 통합 GUI가 열리고, `refresh_stock_data.bat` 또는 GUI의 Data Refresh 화면으로 S&P 500 공유 데이터를 갱신할 수 있습니다.

빌드 스크립트는 같은 위치에 `KeumjStockLab.zip`도 생성합니다. 다른 PC에는 zip 파일을 옮긴 뒤 압축을 풀어 사용하면 됩니다.

Technical-analysis-only GUI:

```cmd
run_pipeline_stock_technical_web_gui.cmd
```

or:

```powershell
.\run_pipeline_stock_technical_web_gui.cmd
```

## CLI

```powershell
.venv\Scripts\python.exe -m pipeline_stock --ticker AAPL --forecast-horizon 10 --out-dir outputs\stock_forecast
```

Local CSV override mode:

```powershell
.venv\Scripts\python.exe -m pipeline_stock --prices-csv data\aapl_prices.csv --forecast-horizon 10 --out-dir outputs\stock_forecast
```

or:

```powershell
.venv\Scripts\python.exe -m pipeline_stock --technical-web-gui --technical-host 127.0.0.1 --technical-port 8792
```
