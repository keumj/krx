# Keumj Stock Analysis API

S&P 500 분석 파이프라인의 독립형 API 서비스입니다.

## 주요 기능
- **주가 예측**: 앙상블 모델을 활용한 10일 후 주가 예측
- **재무 분석**: 재무제표 및 밸류에이션 정보 조회
- **리스크 분석**: 변동성, MDD, VaR 등 리스크 지표 분석
- **포트폴리오 관리**: 현재 포트폴리오 상태 및 성과 분석

## 설치 및 실행 방법

1. **필수 패키지 설치**
   ```bash
   pip install -r requirements.txt
   ```

2. **서버 실행**
   ```bash
   python app/main.py
   ```

3. **접속**
   브라우저에서 `http://localhost:8000` 접속