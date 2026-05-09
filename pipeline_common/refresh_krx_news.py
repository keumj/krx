from __future__ import annotations

from pipeline_krx.refresh_news import main, sync_krx_news_articles


__all__ = ["main", "sync_krx_news_articles"]


if __name__ == "__main__":
    raise SystemExit(main())
