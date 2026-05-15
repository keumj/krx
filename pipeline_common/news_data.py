from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
import html
import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlencode, urlparse

import requests

GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search"
NAVER_NEWS_SEARCH_URL = "https://openapi.naver.com/v1/search/news.json"


@dataclass(frozen=True)
class NewsArticle:
    title: str
    link: str
    source: str
    publish_date: datetime | None


def build_google_news_rss_url(query: str, *, hl: str = "en-US", gl: str = "US", ceid: str = "US:en") -> str:
    params = {"q": query, "hl": hl, "gl": gl, "ceid": ceid}
    return f"{GOOGLE_NEWS_RSS_URL}?{urlencode(params)}"


def _google_news_headers() -> dict[str, str]:
    return {
        "User-Agent": "Keumj Notebook Runner/1.0",
        "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
    }


def download_google_news_rss(
    *,
    url: str | None = None,
    query: str | None = None,
    timeout: int = 8,
    hl: str = "en-US",
    gl: str = "US",
    ceid: str = "US:en",
) -> str:
    final_url = str(url or "").strip()
    if not final_url:
        if not query:
            raise ValueError("Either url or query is required")
        final_url = build_google_news_rss_url(query, hl=hl, gl=gl, ceid=ceid)

    session = requests.Session()
    resp = session.get(final_url, headers=_google_news_headers(), timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _parse_pub_date(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        out = parsedate_to_datetime(text)
    except Exception:
        return None
    if out.tzinfo is not None:
        out = out.astimezone().replace(tzinfo=None)
    return out


def parse_google_news_rss_articles(xml_text: str, *, max_items: int = 20) -> list[NewsArticle]:
    root = ET.fromstring(xml_text)
    articles: list[NewsArticle] = []
    for item in root.findall("./channel/item")[:max_items]:
        title = str(item.findtext("title") or "").strip()
        link = str(item.findtext("link") or "").strip()
        source = str(item.findtext("source") or "").strip()
        publish_date = _parse_pub_date(item.findtext("pubDate"))
        if not title or not link:
            continue
        articles.append(
            NewsArticle(
                title=title,
                link=link,
                source=source or "Google News",
                publish_date=publish_date,
            )
        )
    return articles


def fetch_google_news_titles(
    *,
    url: str | None = None,
    query: str | None = None,
    max_items: int = 20,
    timeout: int = 8,
    hl: str = "en-US",
    gl: str = "US",
    ceid: str = "US:en",
) -> list[str]:
    xml_text = download_google_news_rss(url=url, query=query, timeout=timeout, hl=hl, gl=gl, ceid=ceid)
    root = ET.fromstring(xml_text)
    titles = [item.findtext("title") for item in root.findall("./channel/item")[:max_items]]
    return [t for t in titles if t]


def fetch_google_news_articles(
    *,
    url: str | None = None,
    query: str | None = None,
    max_items: int = 20,
    timeout: int = 8,
    hl: str = "en-US",
    gl: str = "US",
    ceid: str = "US:en",
) -> list[NewsArticle]:
    xml_text = download_google_news_rss(url=url, query=query, timeout=timeout, hl=hl, gl=gl, ceid=ceid)
    return parse_google_news_rss_articles(xml_text, max_items=max_items)


def _strip_html(value: str | None) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    return " ".join(text.split()).strip()


def fetch_naver_news_articles(
    *,
    query: str,
    client_id: str,
    client_secret: str,
    max_items: int = 20,
    timeout: int = 8,
    sort: str = "date",
) -> list[NewsArticle]:
    if not str(query or "").strip():
        raise ValueError("query is required")
    if not str(client_id or "").strip() or not str(client_secret or "").strip():
        raise ValueError("Naver news client_id and client_secret are required")

    headers = {
        "X-Naver-Client-Id": str(client_id).strip(),
        "X-Naver-Client-Secret": str(client_secret).strip(),
        "User-Agent": "Keumj Notebook Runner/1.0",
    }
    session = requests.Session()
    articles: list[NewsArticle] = []
    remaining = max(int(max_items), 0)
    start = 1
    while remaining > 0 and start <= 1000:
        display = min(100, remaining)
        params = {"query": query, "display": display, "start": start, "sort": sort}
        resp = session.get(NAVER_NEWS_SEARCH_URL, headers=headers, params=params, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("items") or []
        if not items:
            break
        for item in items:
            title = _strip_html(item.get("title"))
            link = str(item.get("originallink") or item.get("link") or "").strip()
            source_link = str(item.get("link") or "").strip()
            publish_date = _parse_pub_date(item.get("pubDate"))
            if not title or not link:
                continue
            articles.append(
                NewsArticle(
                    title=title,
                    link=link,
                    source="Naver News" if not source_link else f"Naver News:{urlparse(source_link).netloc}",
                    publish_date=publish_date,
                )
            )
        fetched = len(items)
        if fetched < display:
            break
        remaining -= fetched
        start += fetched
    return articles[:max_items]


def is_google_news_rss_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.netloc.lower() != "news.google.com":
        return False
    if parsed.path.rstrip("/") != "/rss/search":
        return False
    params = parse_qs(parsed.query)
    return "q" in params
