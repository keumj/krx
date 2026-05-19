from __future__ import annotations

import csv
import html
import os
import re
import sqlite3
import threading
from pathlib import Path

from bs4 import BeautifulSoup

from app.settings import settings


_TICKER_CODE_RE = re.compile(r"^\d{6}$")
_COMPANY_MAP_LOCK = threading.RLock()
_COMPANY_MAP: dict[str, str] | None = None


def rewrite_links(page: str, replacements: dict[str, str]) -> str:
    out = page
    for old, new in replacements.items():
        out = out.replace(old, new)
    return out


def _company_name_map() -> dict[str, str]:
    global _COMPANY_MAP
    with _COMPANY_MAP_LOCK:
        if _COMPANY_MAP is not None:
            return _COMPANY_MAP
        out: dict[str, str] = {}
        sqlite_candidates = [
            os.getenv("KRX_SHARED_PRICES_SQLITE_PATH", "").strip(),
            os.getenv("KRX_SHARED_SQLITE_PATH", "").strip(),
            "data/krx_shared_db/krx_shared_prices.sqlite",
        ]
        for raw_path in sqlite_candidates:
            if not raw_path:
                continue
            path = Path(raw_path)
            if not path.is_file():
                continue
            try:
                conn = sqlite3.connect(str(path))
                try:
                    rows = conn.execute(
                        "SELECT symbol, COALESCE(name_kr, ''), COALESCE(name_en, '') FROM securities"
                    ).fetchall()
                finally:
                    conn.close()
                for symbol, name_kr, name_en in rows:
                    code = str(symbol or "").strip().upper()
                    name = str(name_kr or name_en or "").strip()
                    if _TICKER_CODE_RE.match(code) and name and name != code:
                        out[code] = name
                if out:
                    break
            except Exception:
                continue

        if not out:
            for raw_path in [
                os.getenv("KRX_COMPONENTS_CSV_PATH", "").strip(),
                "data/krx_components_full.csv",
                "data/krx_components.csv",
            ]:
                if not raw_path:
                    continue
                path = Path(raw_path)
                if not path.is_file():
                    continue
                try:
                    with path.open("r", encoding="utf-8-sig", newline="") as fh:
                        for row in csv.DictReader(fh):
                            code = str(row.get("Symbol") or row.get("symbol") or "").strip().upper()
                            name = str(
                                row.get("Name")
                                or row.get("name")
                                or row.get("name_kr")
                                or row.get("NameKr")
                                or row.get("종목명")
                                or ""
                            ).strip()
                            if _TICKER_CODE_RE.match(code) and name and name != code:
                                out[code] = name
                    if out:
                        break
                except Exception:
                    continue
        _COMPANY_MAP = out
        return _COMPANY_MAP


def inject_ticker_tooltips(page: str) -> str:
    marker = "data-ticker-company-tooltip"
    if marker in page:
        return page
    company_map = _company_name_map()
    if not company_map:
        return page

    try:
        soup = BeautifulSoup(page, "html.parser")
    except Exception:
        return page

    for cell in soup.find_all(["td", "th"]):
        if cell.find(attrs={"data-ticker-company": True}) is not None:
            continue
        text = cell.get_text(strip=True)
        if not _TICKER_CODE_RE.match(text):
            continue
        company = company_map.get(text)
        if not company:
            continue
        cell["data-ticker-company"] = company
        existing_class = cell.get("class") or []
        if isinstance(existing_class, str):
            existing_class = existing_class.split()
        if "ticker-company-tip" not in existing_class:
            cell["class"] = [*existing_class, "ticker-company-tip"]
        cell["title"] = company

    tooltip_style = """
  <style data-ticker-company-tooltip>
    [data-ticker-company] {
      cursor: help;
      text-decoration: underline dotted rgba(17, 24, 39, 0.32);
      text-underline-offset: 3px;
    }
    .ticker-company-tooltip {
      position: fixed;
      left: 0;
      top: 0;
      z-index: 10000;
      max-width: min(320px, calc(100vw - 24px));
      padding: 7px 10px;
      border-radius: 8px;
      background: rgba(17, 24, 39, 0.95);
      color: #fff;
      border: 1px solid rgba(255, 255, 255, 0.16);
      box-shadow: 0 14px 28px rgba(15, 23, 42, 0.22);
      font: 12px/1.35 "Segoe UI", "Noto Sans KR", sans-serif;
      pointer-events: none;
      opacity: 0;
      transform: translate3d(-9999px, -9999px, 0);
      transition: opacity 100ms ease;
      white-space: nowrap;
    }
    .ticker-company-tooltip.is-visible {
      opacity: 1;
    }
  </style>
"""
    tooltip_script = """
  <div class="ticker-company-tooltip" id="ticker-company-tooltip" aria-hidden="true"></div>
  <script data-ticker-company-tooltip>
    (() => {
      const tooltip = document.getElementById("ticker-company-tooltip");
      if (!tooltip) return;

      const position = (event) => {
        const rect = tooltip.getBoundingClientRect();
        const offsetX = 14;
        const offsetY = 18;
        const maxX = Math.max(window.innerWidth - rect.width - 12, 12);
        const maxY = Math.max(window.innerHeight - rect.height - 12, 12);
        const x = Math.min(Math.max(event.clientX + offsetX, 12), maxX);
        const y = Math.min(Math.max(event.clientY + offsetY, 12), maxY);
        tooltip.style.transform = `translate3d(${x}px, ${y}px, 0)`;
      };

      const show = (target, event) => {
        const company = target.getAttribute("data-ticker-company");
        if (!company) return;
        tooltip.textContent = company;
        tooltip.classList.add("is-visible");
        tooltip.setAttribute("aria-hidden", "false");
        position(event);
      };

      const hide = () => {
        tooltip.classList.remove("is-visible");
        tooltip.setAttribute("aria-hidden", "true");
        tooltip.style.transform = "translate3d(-9999px, -9999px, 0)";
      };

      document.addEventListener("mouseover", (event) => {
        const target = event.target instanceof Element ? event.target.closest("[data-ticker-company]") : null;
        if (target) show(target, event);
      });
      document.addEventListener("mousemove", (event) => {
        if (tooltip.classList.contains("is-visible")) position(event);
      }, { passive: true });
      document.addEventListener("mouseout", (event) => {
        const target = event.target instanceof Element ? event.target.closest("[data-ticker-company]") : null;
        if (target && !target.contains(event.relatedTarget)) hide();
      });
      document.addEventListener("focusin", (event) => {
        const target = event.target instanceof Element ? event.target.closest("[data-ticker-company]") : null;
        if (!target) return;
        const rect = target.getBoundingClientRect();
        show(target, { clientX: rect.left, clientY: rect.bottom });
      });
      document.addEventListener("focusout", hide);
      window.addEventListener("pagehide", hide);
    })();
  </script>
"""
    if soup.head is not None:
        soup.head.append(BeautifulSoup(tooltip_style, "html.parser"))
    else:
        soup.insert(0, BeautifulSoup(tooltip_style, "html.parser"))
    if soup.body is not None:
        soup.body.append(BeautifulSoup(tooltip_script, "html.parser"))
    else:
        soup.append(BeautifulSoup(tooltip_script, "html.parser"))
    return str(soup)


def _service_nav_html(*, active: str = "", admin: bool = False) -> str:
    active_key = str(active or "").strip().lower()
    active_class = {
        "portfolio": "active" if active_key == "portfolio" else "",
        "stock": "active" if active_key == "stock" else "",
        "news": "active" if active_key == "news" else "",
        "macro": "active" if active_key == "macro" else "",
        "admin": "active" if active_key == "admin" else "",
    }
    admin_link = f'<a class="{active_class["admin"]}" href="/admin/users">사용자 관리</a>' if admin else ""
    api_link = '<a href="/docs">API</a>' if admin else ""
    macro_link = f'<a class="{active_class["macro"]}" href="/macro/overview">거시 분석</a>' if settings.enable_macro else ""
    return f"""
        <a class="{active_class["portfolio"]}" href="/portfolio/overview">포트폴리오</a>
        <a class="{active_class["stock"]}" href="/stock/financials">종목 분석</a>
        <a class="{active_class["news"]}" href="/stock-news/overview">뉴스 분석</a>
        {macro_link}
        {admin_link}
        {api_link}
    """


def _service_top_css() -> str:
    return """
  <style data-service-top-nav>
    :root {
      --service-nav-line: #d7e0ea;
      --service-nav-brand: #111827;
    }
    .service-top {
      position: sticky;
      top: 0;
      z-index: 2000;
      background: rgba(255,255,255,.96);
      border-bottom: 1px solid var(--service-nav-line);
      backdrop-filter: blur(10px);
    }
    .service-top-inner {
      width: 100%;
      max-width: 1460px;
      margin: 0 auto;
      padding: 10px 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .service-brand {
      color: var(--service-nav-brand);
      font-weight: 750;
      letter-spacing: 0;
      white-space: nowrap;
      text-decoration: none;
    }
    .service-nav {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .service-nav a {
      color: var(--service-nav-brand);
      border: 1px solid var(--service-nav-line);
      background: #fff;
      text-decoration: none;
      border-radius: 8px;
      padding: 7px 11px;
      font-size: 13px;
      line-height: 1.25;
    }
    .service-nav a.active {
      background: var(--service-nav-brand);
      color: #fff;
      border-color: var(--service-nav-brand);
    }
    @media (max-width: 900px) {
      .service-top-inner {
        align-items: flex-start;
        flex-direction: column;
      }
      .service-nav {
        justify-content: flex-start;
      }
    }
  </style>
"""


def _service_top_markup(*, active: str = "", admin: bool = False) -> str:
    return f"""
  <header class="service-top" data-service-top-nav>
    <div class="service-top-inner">
      <a class="service-brand" href="/">Keumj Portfolio Lab</a>
      <nav class="service-nav">{_service_nav_html(active=active, admin=admin)}</nav>
    </div>
  </header>
"""


def add_service_top_nav(page: str, *, active: str = "", admin: bool = False) -> str:
    if "data-service-top-nav" in page:
        return inject_ticker_tooltips(page)
    css = _service_top_css()
    markup = _service_top_markup(active=active, admin=admin)
    out = page.replace("</head>", css + "\n</head>", 1) if "</head>" in page else css + page
    if "<body>" in out:
        return inject_ticker_tooltips(out.replace("<body>", "<body>" + markup, 1))
    if "<body " in out:
        body_end = out.find(">", out.find("<body "))
        if body_end != -1:
            return inject_ticker_tooltips(out[: body_end + 1] + markup + out[body_end + 1 :])
    return inject_ticker_tooltips(markup + out)


def inject_busy_cursor_overlay(page: str) -> str:
    marker = "data-busy-cursor-overlay"
    if marker in page:
        return page

    overlay_style = """
  <style data-busy-cursor-overlay>
    .busy-cursor-overlay {
      position: fixed;
      left: 0;
      top: 0;
      z-index: 9999;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
      padding: 7px 11px;
      border-radius: 999px;
      background: rgba(17, 24, 39, 0.94);
      color: #fff;
      box-shadow: 0 14px 28px rgba(15, 23, 42, 0.22);
      border: 1px solid rgba(255, 255, 255, 0.14);
      pointer-events: none;
      opacity: 0;
      transform: translate3d(-9999px, -9999px, 0) scale(0.96);
      transition: opacity 140ms ease, transform 140ms ease;
      white-space: nowrap;
      font-family: "Segoe UI", "Noto Sans KR", sans-serif;
    }
    .busy-cursor-overlay.is-visible {
      opacity: 1;
    }
    .busy-cursor-overlay__spinner {
      width: 14px;
      height: 14px;
      border-radius: 50%;
      border: 1.5px solid rgba(255, 255, 255, 0.32);
      border-top-color: #fff;
      animation: busy-cursor-spin 0.8s linear infinite;
      flex: 0 0 auto;
    }
    .busy-cursor-overlay__text {
      font-size: 12px;
      font-weight: 650;
      letter-spacing: -0.01em;
    }
    body.busy-cursor-active,
    body.busy-cursor-active * {
      cursor: progress !important;
    }
    @keyframes busy-cursor-spin {
      to { transform: rotate(360deg); }
    }
  </style>
"""
    overlay_markup = """
  <div class="busy-cursor-overlay" id="busy-cursor-overlay" aria-live="polite" aria-hidden="true">
    <span class="busy-cursor-overlay__spinner" aria-hidden="true"></span>
    <span class="busy-cursor-overlay__text" id="busy-cursor-overlay-text">실행중...</span>
  </div>
  <script data-busy-cursor-overlay>
    (() => {
      const overlay = document.getElementById("busy-cursor-overlay");
      const textEl = document.getElementById("busy-cursor-overlay-text");
      if (!overlay || !textEl) return;

      const state = {
        active: false,
        x: Math.max(window.innerWidth * 0.5, 24),
        y: Math.max(window.innerHeight * 0.35, 24),
      };

      const trackedButtons = new WeakMap();

      const positionOverlay = () => {
        const rect = overlay.getBoundingClientRect();
        const offsetX = 18;
        const offsetY = 22;
        const maxX = Math.max(window.innerWidth - rect.width - 12, 12);
        const maxY = Math.max(window.innerHeight - rect.height - 12, 12);
        const nextX = Math.min(Math.max(state.x + offsetX, 12), maxX);
        const nextY = Math.min(Math.max(state.y + offsetY, 12), maxY);
        overlay.style.transform = `translate3d(${nextX}px, ${nextY}px, 0) scale(${state.active ? 1 : 0.96})`;
      };

      const setPointer = (x, y) => {
        state.x = x;
        state.y = y;
        if (state.active) {
          positionOverlay();
        }
      };

      const isAnalysisIntent = (form) => {
        if (!(form instanceof HTMLFormElement)) return false;
        if (form.dataset.noBusyCursor === "true") return false;

        const method = (form.getAttribute("method") || "get").toLowerCase();
        const action = (form.getAttribute("action") || window.location.pathname).toLowerCase();
        const intentField = form.querySelector('input[name="intent"]');
        const intent = (intentField ? intentField.value : "").trim().toLowerCase();

        if (method === "get" && ["run", "analyze", "refresh"].includes(intent)) {
          return true;
        }

        if (method !== "post") return false;

        if (action.includes("/stock/run")) return true;
        if (action.includes("/stock-news/run-")) return true;
        if (action.includes("/run_virtual_trade")) return true;
        if (action.includes("/run_refresh")) return true;
        return false;
      };

      const isPageNavigationLink = (event, link) => {
        if (!(link instanceof HTMLAnchorElement)) return false;
        if (event.defaultPrevented) return false;
        if (event.button !== 0) return false;
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;
        if (link.dataset.noBusyCursor === "true") return false;
        if (link.hasAttribute("download")) return false;

        const target = (link.getAttribute("target") || "").trim().toLowerCase();
        if (target && target !== "_self") return false;

        const rawHref = (link.getAttribute("href") || "").trim();
        if (!rawHref || rawHref.startsWith("#")) return false;
        if (/^(javascript|mailto|tel):/i.test(rawHref)) return false;

        let url;
        try {
          url = new URL(rawHref, window.location.href);
        } catch (err) {
          return false;
        }
        if (url.origin !== window.location.origin) return false;
        if (
          url.pathname === window.location.pathname &&
          url.search === window.location.search &&
          url.hash
        ) {
          return false;
        }
        return true;
      };

      const resolveLabel = (form, submitter) => {
        return "실행중...";
      };

      const activate = (label) => {
        state.active = true;
        textEl.textContent = label;
        overlay.classList.add("is-visible");
        overlay.setAttribute("aria-hidden", "false");
        document.body.classList.add("busy-cursor-active");
        positionOverlay();
      };

      const deactivate = () => {
        state.active = false;
        overlay.classList.remove("is-visible");
        overlay.setAttribute("aria-hidden", "true");
        document.body.classList.remove("busy-cursor-active");
        positionOverlay();
      };

      document.addEventListener("mousemove", (event) => {
        setPointer(event.clientX, event.clientY);
      }, { passive: true });

      document.addEventListener("pointerdown", (event) => {
        setPointer(event.clientX, event.clientY);
      }, { passive: true });

      document.addEventListener("click", (event) => {
        const target = event.target instanceof Element ? event.target.closest("button, input[type=submit]") : null;
        if (target) {
          trackedButtons.set(target.form || document.body, target);
        }
      }, true);

      document.addEventListener("click", (event) => {
        const link = event.target instanceof Element ? event.target.closest("a[href]") : null;
        if (isPageNavigationLink(event, link)) {
          activate(resolveLabel(null, null));
        }
      });

      document.addEventListener("submit", (event) => {
        const form = event.target;
        if (!isAnalysisIntent(form)) return;
        const submitter = event.submitter || trackedButtons.get(form) || null;
        activate(resolveLabel(form, submitter));
      }, true);

      window.addEventListener("pageshow", deactivate);
      window.addEventListener("pagehide", deactivate);
      window.addEventListener("focus", () => {
        if (!document.hidden) deactivate();
      });

      positionOverlay();
    })();
  </script>
"""

    page = page.replace("</head>", overlay_style + "\n</head>", 1) if "</head>" in page else overlay_style + page
    if "</body>" in page:
        return page.replace("</body>", overlay_markup + "\n</body>", 1)
    return page + overlay_markup


def shell(
    title: str,
    body: str,
    *,
    active: str = "",
    admin: bool = False,
    start_page_only: bool = False,
) -> str:
    default_nav = _service_nav_html(active=active, admin=admin)
    return inject_busy_cursor_overlay(inject_ticker_tooltips(f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #f5f7fa;
      --panel: #ffffff;
      --line: #d7e0ea;
      --text: #1f2937;
      --muted: #667085;
      --brand: #111827;
      --accent: #0f766e;
      --danger: #a12626;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; color: var(--text); background: var(--bg); font-family: "Segoe UI", "Noto Sans KR", sans-serif; }}
    .service-top {{ position: sticky; top: 0; z-index: 20; background: rgba(255,255,255,.96); border-bottom: 1px solid var(--line); }}
    .service-top-inner {{ width: 100%; max-width: 1460px; margin: 0 auto; padding: 10px 20px; display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
    .service-brand {{ color: var(--brand); font-weight: 750; letter-spacing: 0; white-space: nowrap; text-decoration: none; }}
    .service-nav {{ display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }}
    .service-nav a {{ color: var(--brand); border: 1px solid var(--line); background: #fff; text-decoration: none; border-radius: 8px; padding: 7px 11px; font-size: 13px; }}
    .service-nav a.active {{ background: var(--brand); color: #fff; border-color: var(--brand); }}
    .service-main {{ width: 100%; max-width: 1460px; margin: 0 auto; padding: 16px 20px 30px; }}
    .service-card {{ max-width: 100%; min-width: 0; overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }}
    .service-grid {{ display: grid; grid-template-columns: repeat(3, minmax(220px, 1fr)); gap: 12px; }}
    .service-grid a {{ display: block; color: var(--text); text-decoration: none; }}
    .service-grid h3 {{ margin: 0 0 6px; font-size: 16px; }}
    .service-grid p {{ margin: 0; color: var(--muted); font-size: 13px; line-height: 1.45; }}
    .service-stack {{ display: grid; gap: 12px; }}
    .service-login-grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }}
    .service-login-grid h3 {{ margin: 0 0 10px; font-size: 15px; }}
    .service-login-grid label {{ display: block; margin: 10px 0 4px; font-size: 12px; color: var(--muted); }}
    .service-login-grid input {{ width: 100%; border: 1px solid var(--line); border-radius: 8px; padding: 10px; font-size: 14px; }}
    .service-login-grid button {{ margin-top: 14px; }}
    .service-table-wrap {{ width: 100%; max-width: 100%; min-width: 0; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
    .service-table {{ width: max-content; min-width: 100%; border-collapse: collapse; font-size: 13px; line-height: 1.45; }}
    .service-table th, .service-table td {{ border: 1px solid var(--line); padding: 8px; text-align: left; vertical-align: top; white-space: normal; overflow-wrap: break-word; word-break: keep-all; }}
    .service-actions {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
    .service-actions input {{ border: 1px solid var(--line); border-radius: 8px; padding: 9px 10px; font-size: 13px; }}
    .service-button {{ border: 0; background: var(--brand); color: #fff; border-radius: 8px; padding: 9px 13px; cursor: pointer; font-weight: 650; }}
    .service-button.secondary {{ background: var(--accent); }}
    .service-muted {{ color: var(--muted); }}
    .service-error {{ color: var(--danger); white-space: pre-wrap; }}
    @media (max-width: 900px) {{
      .service-top-inner {{ align-items: flex-start; flex-direction: column; }}
      .service-nav {{ justify-content: flex-start; }}
      .service-grid {{ grid-template-columns: 1fr; }}
      .service-login-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header class="service-top" data-service-top-nav>
    <div class="service-top-inner">
      <a class="service-brand" href="/">Keumj Portfolio Lab</a>
      <nav class="service-nav">{default_nav}</nav>
    </div>
  </header>
  <main class="service-main">
    {body}
  </main>
</body>
</html>
"""))
