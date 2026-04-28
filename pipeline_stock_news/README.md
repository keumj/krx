# Stock News Lab

`pipeline_stock_news`는 `data/sp500_shared_db/sp500_shared_prices.sqlite` 안의 `prices`, `news_articles`, `news_articles_market_context`를 직접 읽어 독립적으로 실행되는 뉴스-가격 분석 툴입니다.

핵심 기능:
- 이벤트 스터디: 특정 키워드 뉴스 이후 `N`거래일 수익률 경로 집계
- 뉴스-가격 다이버전스: 감성 방향과 실제 가격 반응이 어긋나는 사례 탐지
- 토픽 모델링: 최근 뉴스 제목을 시장 테마로 묶고 핵심 키워드 요약

실행:

```bash
python -m pipeline_stock_news --web-gui --host localhost --port 8514
```

또는:

```cmd
run_pipeline_stock_news_web_gui.cmd
```

CLI 요약 실행:

```bash
python -m pipeline_stock_news --event-keywords "earnings, guidance" --lookback-days 60 --horizon-days 5
```
