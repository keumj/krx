from __future__ import annotations

import ctypes
import html
import json
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


_APP_TITLE = "SP500 GUI"


@dataclass(frozen=True)
class _BackendConfig:
    key: str
    label: str
    module: str
    host: str
    port: int


@dataclass(frozen=True)
class _PageLink:
    page_id: str
    section_key: str
    title: str
    path: str
    backend_key: str | None = None
    source_label: str | None = None


@dataclass(frozen=True)
class _SectionConfig:
    key: str
    title: str
    page_ids: tuple[str, ...]
    status_backend_keys: tuple[str, ...] = ()


_BACKENDS: tuple[_BackendConfig, ...] = (
    _BackendConfig("stock", "스톡_분석", "pipeline_stock", "localhost", 8512),
    _BackendConfig("stock_news", "스톡_뉴스", "pipeline_stock_news", "localhost", 8514),
    _BackendConfig("portfolio", "포트폴리오", "pipeline_portfolio", "localhost", 8515),
)

_PAGES: tuple[_PageLink, ...] = (
    _PageLink("portfolio-refresh", "data_refresh", "데이터갱신", "/refresh", backend_key="portfolio", source_label="포트폴리오"),
    _PageLink("portfolio-entry", "portfolio", "거래 입력", "/data-entry", backend_key="portfolio"),
    _PageLink("portfolio-overview", "portfolio", "포트폴리오 개요", "/overview", backend_key="portfolio"),
    _PageLink("portfolio-attribution", "portfolio", "성과/Attribution", "/attribution", backend_key="portfolio"),
    _PageLink("portfolio-risk", "portfolio", "리스크", "/risk", backend_key="portfolio"),
    _PageLink("portfolio-scoring", "portfolio", "통합 스코어", "/scoring", backend_key="portfolio"),
    _PageLink("portfolio-virtual", "portfolio", "가상 거래", "/virtual-trade", backend_key="portfolio"),
    _PageLink("portfolio-optimization", "portfolio", "최적화", "/optimization", backend_key="portfolio"),
    _PageLink("news-overview", "stock_news", "뉴스 개요", "/overview", backend_key="stock_news"),
    _PageLink("news-event", "stock_news", "이벤트 스터디", "/event-study", backend_key="stock_news"),
    _PageLink("news-spillover", "stock_news", "섹터 전이", "/sector-spillover", backend_key="stock_news"),
    _PageLink("news-divergence", "stock_news", "뉴스-프라이스 다이버전스", "/divergence", backend_key="stock_news"),
    _PageLink("news-reset", "stock_news", "기대 리셋", "/expectation-reset", backend_key="stock_news"),
    _PageLink("news-volatility", "stock_news", "변동성 레짐", "/volatility-regime", backend_key="stock_news"),
    _PageLink("news-topics", "stock_news", "토픽 모델링", "/topic-modeling", backend_key="stock_news"),
    _PageLink("stock-forecast", "stock", "주가 예측", "/forecast", backend_key="stock"),
    _PageLink("stock-financials", "stock", "재무제표·밸류에이션", "/page2", backend_key="stock"),
    _PageLink("stock-technical", "stock", "기술적 분석", "/page3", backend_key="stock"),
    _PageLink("stock-returns", "stock", "수익률 비교", "/page4", backend_key="stock"),
    _PageLink("stock-risk", "stock", "리스크 대시보드", "/page5", backend_key="stock"),
    _PageLink("stock-factor", "stock", "팩터·레짐 랩", "/factor-regime", backend_key="stock"),
    _PageLink("stock-decision", "stock", "의사결정 대시보드", "/page6", backend_key="stock"),
    _PageLink("stock-wfv", "stock", "워크포워드 검증", "/page8", backend_key="stock"),
)

_SECTIONS: tuple[_SectionConfig, ...] = (
    _SectionConfig("data_refresh", "데이터갱신", ("portfolio-refresh",), ("portfolio",)),
    _SectionConfig("portfolio", "포트폴리오", ("portfolio-entry", "portfolio-overview", "portfolio-attribution", "portfolio-risk", "portfolio-scoring", "portfolio-virtual", "portfolio-optimization"), ("portfolio",)),
    _SectionConfig("stock_news", "스톡_뉴스", ("news-overview", "news-event", "news-spillover", "news-divergence", "news-reset", "news-volatility", "news-topics"), ("stock_news",)),
    _SectionConfig("stock", "스톡_분석", ("stock-forecast", "stock-financials", "stock-technical", "stock-returns", "stock-risk", "stock-factor", "stock-decision", "stock-wfv"), ("stock",)),
)

_spawn_lock = threading.Lock()
_spawned_processes: dict[str, subprocess.Popen[bytes] | subprocess.Popen[str]] = {}
_spawn_inflight: set[str] = set()
_windows_child_job_handle: int | None = None


if os.name == "nt":
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]


    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]


    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]


def _backend_by_key(key: str) -> _BackendConfig:
    for backend in _BACKENDS:
        if backend.key == key:
            return backend
    raise KeyError(key)


def _page_by_id(page_id: str) -> _PageLink:
    for page in _PAGES:
        if page.page_id == page_id:
            return page
    return _PAGES[0]


def _page_url(page: _PageLink) -> str:
    if page.backend_key is None:
        return page.path
    backend = _backend_by_key(page.backend_key)
    return f"http://{backend.host}:{backend.port}{page.path}"


def _section_by_key(key: str) -> _SectionConfig:
    for section in _SECTIONS:
        if section.key == key:
            return section
    return _SECTIONS[0]


def _section_for_page(page: _PageLink) -> _SectionConfig:
    return _section_by_key(page.section_key)


def _page_source_label(page: _PageLink) -> str:
    if page.source_label:
        return page.source_label
    if page.backend_key is None:
        return _APP_TITLE
    return _backend_by_key(page.backend_key).label


def _section_status(section: _SectionConfig) -> tuple[str, bool] | None:
    if not section.status_backend_keys:
        return None
    total = len(section.status_backend_keys)
    online_count = 0
    for backend_key in section.status_backend_keys:
        backend = _backend_by_key(backend_key)
        if _is_port_open(backend.host, backend.port):
            online_count += 1
    if total == 1:
        return ("online" if online_count == 1 else "starting", online_count == 1)
    return (f"{online_count}/{total} online", online_count == total)


def _is_port_open(host: str, port: int, timeout: float = 0.35) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, int(port))) == 0


def _get_windows_child_job() -> int | None:
    if os.name != "nt":
        return None

    global _windows_child_job_handle
    if _windows_child_job_handle is not None:
        return _windows_child_job_handle

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    job_handle = int(kernel32.CreateJobObjectW(None, None))
    if job_handle == 0:
        return None

    limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = bool(
        kernel32.SetInformationJobObject(
            ctypes.c_void_p(job_handle),
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        )
    )
    if not ok:
        kernel32.CloseHandle(ctypes.c_void_p(job_handle))
        return None

    _windows_child_job_handle = job_handle
    return _windows_child_job_handle


def _assign_process_to_windows_job(job_handle: int, process: subprocess.Popen[bytes] | subprocess.Popen[str]) -> None:
    if os.name != "nt":
        return

    process_handle = getattr(process, "_handle", None)
    if process_handle in (None, 0):
        return

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.AssignProcessToJobObject(ctypes.c_void_p(job_handle), ctypes.c_void_p(int(process_handle)))


def _attach_process_to_parent_lifetime(process: subprocess.Popen[bytes] | subprocess.Popen[str]) -> None:
    if os.name != "nt":
        return

    job_handle = _get_windows_child_job()
    if job_handle is None:
        return

    try:
        _assign_process_to_windows_job(job_handle, process)
    except Exception:
        return


def _spawn_backend(backend: _BackendConfig) -> None:
    creationflags = 0
    if os.name == "nt":
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))

    with _spawn_lock:
        existing = _spawned_processes.get(backend.key)
        if existing is not None and existing.poll() is None:
            return
        if backend.key in _spawn_inflight:
            return
        _spawn_inflight.add(backend.key)

    try:
        command = [
            sys.executable,
            "-m",
            backend.module,
            "--web-gui",
            "--host",
            backend.host,
            "--port",
            str(backend.port),
        ]
        if getattr(sys, "frozen", False):
            command = [
                sys.executable,
                "backend",
                backend.key,
                "--host",
                backend.host,
                "--port",
                str(backend.port),
            ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            cwd=os.getcwd(),
            creationflags=creationflags,
            start_new_session=False,
        )
        # Tie spawned backends to the parent process so shell shutdown also reaps them.
        _attach_process_to_parent_lifetime(process)
    except Exception:
        with _spawn_lock:
            _spawn_inflight.discard(backend.key)
        raise

    with _spawn_lock:
        _spawned_processes[backend.key] = process
        _spawn_inflight.discard(backend.key)


def _ensure_backend_running(backend: _BackendConfig) -> None:
    if _is_port_open(backend.host, backend.port):
        return
    _spawn_backend(backend)


def _warm_up_backends() -> None:
    for backend in _BACKENDS:
        _ensure_backend_running(backend)
        time.sleep(0.2)


def _cleanup_spawned_backends() -> None:
    with _spawn_lock:
        items = list(_spawned_processes.items())
        _spawned_processes.clear()
        _spawn_inflight.clear()
    for _, process in items:
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except Exception:
                    process.kill()
        except Exception:
            continue


def _status_payload(selected_page_id: str) -> dict[str, object]:
    selected = _page_by_id(selected_page_id)
    backends: list[dict[str, object]] = []
    for backend in _BACKENDS:
        backends.append(
            {
                "key": backend.key,
                "label": backend.label,
                "host": backend.host,
                "port": backend.port,
                "online": _is_port_open(backend.host, backend.port),
            }
        )
    return {
        "selected_page_id": selected.page_id,
        "selected_title": selected.title,
        "selected_backend": selected.backend_key or "",
        "selected_section": selected.section_key,
        "selected_url": _page_url(selected),
        "backends": backends,
    }


def _base_css() -> str:
    return """
    :root {
      --bg: #eef2f6;
      --panel: #f8fbfd;
      --card: #ffffff;
      --line: #cfd9e3;
      --line-strong: #a8b7c7;
      --text: #17212b;
      --muted: #617180;
      --brand: #0f4c81;
      --brand-soft: #dceaf7;
      --ok: #2d7a46;
      --ok-soft: #e8f5ec;
      --warn: #b26a00;
      --shadow: 0 10px 30px rgba(15, 23, 42, 0.08);
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; }
    body {
      margin: 0;
      background:
        radial-gradient(circle at top left, rgba(15, 76, 129, 0.08), transparent 28%),
        linear-gradient(180deg, #f7fafc 0%, var(--bg) 100%);
      color: var(--text);
      font-family: "Segoe UI", "Noto Sans KR", sans-serif;
    }
    .app {
      display: grid;
      grid-template-columns: 15% 85%;
      min-height: 100vh;
    }
    .sidebar {
      position: relative;
      border-right: 1px solid var(--line);
      background: linear-gradient(180deg, #f7fbff 0%, #eef4f9 100%);
      padding: 18px 12px 18px 14px;
      overflow-y: auto;
    }
    .content {
      min-width: 0;
      padding: 14px;
      display: grid;
      grid-template-rows: auto 1fr;
      gap: 12px;
    }
    .brand {
      margin-bottom: 16px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--line);
    }
    .brand h1 {
      margin: 0 0 4px;
      font-size: 20px;
      line-height: 1.2;
    }
    .brand p {
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }
    .section-group {
      position: relative;
      margin: 0 0 18px 0;
      padding-left: 12px;
    }
    .section-group::before {
      content: "";
      position: absolute;
      left: 2px;
      top: 26px;
      bottom: 8px;
      width: 2px;
      background: linear-gradient(180deg, var(--line-strong), transparent);
    }
    .section-toggle {
      width: 100%;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin: 0 0 10px 0;
      padding: 0;
      background: transparent;
      border: 0;
      color: var(--muted);
      cursor: pointer;
      text-align: left;
    }
    .section-title {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 12px;
      font-weight: 700;
      color: var(--muted);
      letter-spacing: 0.02em;
      text-transform: uppercase;
    }
    .section-caret {
      width: 10px;
      height: 10px;
      border-right: 2px solid var(--muted);
      border-bottom: 2px solid var(--muted);
      transform: rotate(45deg);
      transition: transform 0.15s ease;
      flex: 0 0 auto;
      margin-right: 2px;
    }
    .section-group.expanded .section-caret {
      transform: rotate(225deg);
      margin-top: 5px;
    }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 3px 8px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: #fff;
      font-size: 11px;
      font-weight: 600;
      color: var(--muted);
    }
    .status-pill.online {
      border-color: #b9dec5;
      background: var(--ok-soft);
      color: var(--ok);
    }
    .status-dot {
      width: 7px;
      height: 7px;
      border-radius: 999px;
      background: var(--warn);
      flex: 0 0 auto;
    }
    .status-pill.online .status-dot {
      background: var(--ok);
    }
    .waterfall {
      display: grid;
      gap: 8px;
    }
    .section-group:not(.expanded) .waterfall {
      display: none;
    }
    .nav-link {
      position: relative;
      display: block;
      width: 100%;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.85);
      color: var(--text);
      text-align: left;
      border-radius: 12px;
      padding: 10px 10px 10px 14px;
      cursor: pointer;
      transition: transform 0.15s ease, border-color 0.15s ease, background 0.15s ease, box-shadow 0.15s ease;
      box-shadow: 0 3px 10px rgba(15, 23, 42, 0.04);
      font-size: 13px;
      line-height: 1.35;
    }
    .nav-link::before {
      content: "";
      position: absolute;
      left: -10px;
      top: 50%;
      width: 10px;
      height: 2px;
      transform: translateY(-50%);
      background: var(--line-strong);
    }
    .nav-link:hover {
      transform: translateX(3px);
      border-color: #9db8d4;
      background: #fff;
    }
    .nav-link.active {
      border-color: var(--brand);
      background: linear-gradient(180deg, #f9fcff 0%, var(--brand-soft) 100%);
      box-shadow: 0 10px 18px rgba(15, 76, 129, 0.14);
    }
    .nav-link.active::after {
      content: "";
      position: absolute;
      inset: 0 auto 0 0;
      width: 4px;
      border-radius: 12px 0 0 12px;
      background: var(--brand);
    }
    .nav-link small {
      display: block;
      margin-top: 4px;
      color: var(--muted);
      font-size: 11px;
    }
    .toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      background: rgba(255, 255, 255, 0.82);
      border: 1px solid var(--line);
      border-radius: 14px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(8px);
    }
    .toolbar-left {
      min-width: 0;
    }
    .toolbar-left h2 {
      margin: 0 0 4px;
      font-size: 20px;
      line-height: 1.25;
    }
    .toolbar-left p {
      margin: 0;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }
    .toolbar-right {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .toolbar-btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 40px;
      padding: 0 14px;
      border-radius: 10px;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--text);
      text-decoration: none;
      font-size: 13px;
      font-weight: 600;
    }
    .toolbar-btn.primary {
      border-color: var(--brand);
      background: var(--brand);
      color: #fff;
    }
    .viewer-card {
      position: relative;
      min-height: 0;
      border: 1px solid var(--line);
      border-radius: 18px;
      overflow: hidden;
      background: var(--card);
      box-shadow: var(--shadow);
    }
    .viewer-frame {
      width: 100%;
      height: calc(100vh - 112px);
      border: 0;
      background: #fff;
    }
    .viewer-overlay {
      position: absolute;
      inset: 14px 14px auto 14px;
      display: none;
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid #e7d0a6;
      background: #fff7e7;
      color: #8a5a00;
      font-size: 12px;
      line-height: 1.45;
      z-index: 2;
    }
    .viewer-overlay.show {
      display: block;
    }
    @media (max-width: 1180px) {
      .app {
        grid-template-columns: 240px 1fr;
      }
    }
    @media (max-width: 820px) {
      .app {
        grid-template-columns: 1fr;
        grid-template-rows: auto 1fr;
      }
      .sidebar {
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }
      .content {
        padding-top: 10px;
      }
      .viewer-frame {
        height: calc(100vh - 360px);
      }
    }
    """


def _sidebar_html(selected_page_id: str) -> str:
    selected = _page_by_id(selected_page_id)
    parts: list[str] = []
    for section in _SECTIONS:
        pages = [_page_by_id(page_id) for page_id in section.page_ids]
        expanded_css = " expanded" if section.key == selected.section_key else ""
        status = _section_status(section)
        status_html = ""
        if status is not None:
            status_label, is_online = status
            status_css = "status-pill online" if is_online else "status-pill"
            status_html = (
                f'<span class="{status_css}" data-section-pill="{html.escape(section.key)}">'
                '<span class="status-dot"></span>'
                f'<span data-section-text="{html.escape(section.key)}">{html.escape(status_label)}</span>'
                "</span>"
            )
        links: list[str] = []
        for page in pages:
            active_css = " active" if page.page_id == selected.page_id else ""
            links.append(
                (
                    f'<button class="nav-link{active_css}" '
                    f'data-page-id="{html.escape(page.page_id)}" '
                    f'data-page-url="{html.escape(_page_url(page))}" '
                    f'data-page-title="{html.escape(page.title)}" '
                    f'data-backend-key="{html.escape(page.backend_key or "")}" '
                    f'data-backend-label="{html.escape(section.title)}" '
                    f'type="button">'
                    f"{html.escape(page.title)}"
                    f'<small>{html.escape(page.path)}</small>'
                    "</button>"
                )
            )
        parts.append(
            (
                f'<section class="section-group{expanded_css}" data-section-panel="{html.escape(section.key)}">'
                f'<button class="section-toggle" type="button" data-section-toggle="{html.escape(section.key)}" aria-expanded="{str(section.key == selected.section_key).lower()}">'
                f'<span class="section-title">{html.escape(section.title)}</span>'
                '<span style="display:flex; align-items:center; gap:8px;">'
                f"{status_html}"
                '<span class="section-caret"></span>'
                "</span>"
                "</button>"
                f'<div class="waterfall">{"".join(links)}</div>'
                "</section>"
            )
        )
    return "".join(parts)


def _main_page(selected_page_id: str) -> str:
    selected = _page_by_id(selected_page_id)
    selected_source_label = _page_source_label(selected)
    selected_url = _page_url(selected)
    backend_online = True if selected.backend_key is None else _is_port_open(_backend_by_key(selected.backend_key).host, _backend_by_key(selected.backend_key).port)
    initial_frame_src = selected_url if backend_online else "about:blank"
    overlay_css = "viewer-overlay show" if not backend_online else "viewer-overlay"
    overlay_text = (
        f"{selected_source_label} 서버를 확인 중입니다. 잠시 후 자동으로 내용이 보이면 정상입니다."
        if not backend_online
        else ""
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(_APP_TITLE)}</title>
  <style>{_base_css()}</style>
</head>
<body>
  <div class="app">
    <aside class="sidebar">
      <div class="brand">
        <h1>SP500</h1>
        <p>데이터갱신, 포트폴리오, 스톡_뉴스, 스톡_분석 화면을 한곳에서 보고, 왼쪽 워터폴 목록으로 페이지를 전환합니다.</p>
      </div>
      {_sidebar_html(selected_page_id)}
    </aside>
    <main class="content">
      <section class="toolbar">
        <div class="toolbar-left">
          <h2 id="page-title">{html.escape(selected.title)}</h2>
          <p id="page-subtitle">{html.escape(selected_source_label)} · {html.escape(selected_url)}</p>
        </div>
        <div class="toolbar-right">
          <button class="toolbar-btn" id="refresh-frame" type="button">현재 페이지 새로고침</button>
          <a class="toolbar-btn primary" id="open-direct" href="{html.escape(selected_url)}" target="_blank" rel="noreferrer">새 탭에서 열기</a>
        </div>
      </section>
      <section class="viewer-card">
        <div class="{overlay_css}" id="viewer-overlay">{html.escape(overlay_text)}</div>
        <iframe
          id="viewer-frame"
          class="viewer-frame"
          src="{html.escape(initial_frame_src)}"
          title="{html.escape(selected.title)}"
          loading="eager"
          referrerpolicy="no-referrer"
          data-backend-key="{html.escape(selected.backend_key or "")}"
          data-pending-url="{html.escape('' if backend_online else selected_url)}"
        ></iframe>
      </section>
    </main>
  </div>
  <script>
    (function () {{
      const frameEl = document.getElementById("viewer-frame");
      const titleEl = document.getElementById("page-title");
      const subtitleEl = document.getElementById("page-subtitle");
      const directEl = document.getElementById("open-direct");
      const overlayEl = document.getElementById("viewer-overlay");
      const refreshBtn = document.getElementById("refresh-frame");
      const backendState = {{}};

      function setSectionExpanded(panelEl, expanded) {{
        if (!panelEl) {{
          return;
        }}
        panelEl.classList.toggle("expanded", Boolean(expanded));
        const toggleEl = panelEl.querySelector(".section-toggle");
        if (toggleEl) {{
          toggleEl.setAttribute("aria-expanded", expanded ? "true" : "false");
        }}
      }}

      function getCurrentPageId() {{
        const activeButton = document.querySelector(".nav-link.active");
        if (activeButton && activeButton.dataset.pageId) {{
          return String(activeButton.dataset.pageId);
        }}
        return String(new URL(window.location.href).searchParams.get("page") || "{html.escape(selected.page_id)}");
      }}

      function updateSelection(buttonEl, pushHistory) {{
        if (!buttonEl) {{
          return;
        }}
        document.querySelectorAll(".nav-link.active").forEach((node) => node.classList.remove("active"));
        buttonEl.classList.add("active");
        const panelEl = buttonEl.closest("[data-section-panel]");
        if (panelEl) {{
          setSectionExpanded(panelEl, true);
        }}
        const nextUrl = buttonEl.dataset.pageUrl || "";
        const nextTitle = buttonEl.dataset.pageTitle || "";
        const backendKey = buttonEl.dataset.backendKey || "";
        const backendLabel = buttonEl.dataset.backendLabel || "";
        const nextPageId = buttonEl.dataset.pageId || "";
        titleEl.textContent = nextTitle;
        subtitleEl.textContent = backendLabel + " · " + nextUrl;
        directEl.href = nextUrl;
        frameEl.title = nextTitle;
        frameEl.dataset.backendKey = backendKey;
        const backendOnline = backendKey ? Boolean(backendState[backendKey]) : true;
        if (backendOnline) {{
          frameEl.dataset.pendingUrl = "";
          frameEl.src = nextUrl;
          overlayEl.classList.remove("show");
        }} else {{
          frameEl.dataset.pendingUrl = nextUrl;
          frameEl.src = "about:blank";
          overlayEl.classList.add("show");
          overlayEl.textContent = backendLabel + " 서버를 확인 중입니다. 잠시 후 자동으로 내용이 보이면 정상입니다.";
        }}
        if (pushHistory) {{
          const url = new URL(window.location.href);
          url.searchParams.set("page", nextPageId);
          window.history.replaceState({{ pageId: nextPageId }}, "", url.toString());
        }}
      }}

      document.querySelectorAll(".section-toggle").forEach((toggleEl) => {{
        toggleEl.addEventListener("click", function (event) {{
          event.preventDefault();
          event.stopPropagation();
          const sectionKey = toggleEl.dataset.sectionToggle || "";
          const panelEl = document.querySelector('[data-section-panel="' + sectionKey + '"]');
          if (!panelEl) {{
            return;
          }}
          const nextExpanded = !panelEl.classList.contains("expanded");
          setSectionExpanded(panelEl, nextExpanded);
        }});
      }});

      document.querySelectorAll(".nav-link").forEach((buttonEl) => {{
        buttonEl.addEventListener("click", function (event) {{
          event.preventDefault();
          event.stopPropagation();
          updateSelection(buttonEl, true);
        }});
      }});

      refreshBtn.addEventListener("click", function () {{
        const pendingUrl = String(frameEl.dataset.pendingUrl || "");
        frameEl.src = pendingUrl || directEl.href;
      }});

      frameEl.addEventListener("load", function () {{
        overlayEl.classList.remove("show");
      }});

      async function pollStatus() {{
        try {{
          const res = await fetch("/api/status?page=" + encodeURIComponent(getCurrentPageId()), {{ cache: "no-store" }});
          if (!res.ok) {{
            return;
          }}
          const data = await res.json();
          const backends = Array.isArray(data.backends) ? data.backends : [];
          backends.forEach((backend) => {{
            backendState[String(backend.key || "")] = Boolean(backend.online);
          }});

          const sectionBackendMap = {{
            "data_refresh": ["portfolio"],
            "portfolio": ["portfolio"],
            "stock_news": ["stock_news"],
            "stock": ["stock"]
          }};
          Object.keys(sectionBackendMap).forEach((sectionKey) => {{
            const keys = sectionBackendMap[sectionKey];
            const onlineCount = keys.filter((key) => backendState[String(key)]).length;
            const total = keys.length;
            const pillEl = document.querySelector('[data-section-pill="' + sectionKey + '"]');
            const textEl = document.querySelector('[data-section-text="' + sectionKey + '"]');
            if (!pillEl || !textEl) {{
              return;
            }}
            if (total === 1) {{
              if (onlineCount === 1) {{
                pillEl.classList.add("online");
                textEl.textContent = "online";
              }} else {{
                pillEl.classList.remove("online");
                textEl.textContent = "starting";
              }}
            }} else {{
              textEl.textContent = String(onlineCount) + "/" + String(total) + " online";
              pillEl.classList.toggle("online", onlineCount === total);
            }}
          }});

          const selectedPageId = String(data.selected_page_id || "");
          const selectedBackend = String(data.selected_backend || "");
          const selectedButton = document.querySelector('[data-page-id="' + selectedPageId + '"]');
          const backendOnline = selectedBackend ? Boolean(backendState[selectedBackend]) : true;
          if (!backendOnline && selectedButton) {{
            overlayEl.classList.add("show");
            overlayEl.textContent = (selectedButton.dataset.backendLabel || "선택한 화면") + " 서버를 기동하는 중입니다.";
          }} else if (backendOnline) {{
            const pendingUrl = String(frameEl.dataset.pendingUrl || "");
            if (pendingUrl) {{
              frameEl.dataset.pendingUrl = "";
              frameEl.src = pendingUrl;
            }}
          }}
        }} catch (err) {{
        }}
      }}

      pollStatus();
      window.setInterval(pollStatus, 2500);
    }})();
  </script>
</body>
</html>
"""


def launch_web_gui(host: str = "localhost", port: int = 8516, open_browser: bool = False) -> None:
    warmup_thread = threading.Thread(target=_warm_up_backends, daemon=True)
    warmup_thread.start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            selected_page_id = str(query.get("page", ["portfolio-refresh"])[0]).strip() or "portfolio-refresh"
            selected_page = _page_by_id(selected_page_id)

            if path == "/api/status":
                self._send_json(_status_payload(selected_page.page_id))
                return
            if path in ("/", "/index.html"):
                if selected_page.backend_key is not None:
                    _ensure_backend_running(_backend_by_key(selected_page.backend_key))
                self._send_html(_main_page(selected_page.page_id))
                return

            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

        def _send_html(self, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, payload_obj: dict[str, object], status: int = 200) -> None:
            payload = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    server = ThreadingHTTPServer((host, int(port)), Handler)
    print(f"{_APP_TITLE} listening on http://{host}:{port}", flush=True)
    if open_browser:
        try:
            webbrowser.open(f"http://{host}:{port}/")
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        _cleanup_spawned_backends()


run_web_gui = launch_web_gui
