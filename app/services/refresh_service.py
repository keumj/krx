from __future__ import annotations

import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from app.settings import settings
from pipeline_portfolio import web_gui as portfolio_web


@dataclass(frozen=True)
class RefreshJob:
    job_id: str
    label: str
    command: list[str]
    description: str


@dataclass
class JobState:
    status: str = "idle"
    run_id: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    logs: list[str] = field(default_factory=list)
    error: str | None = None


JOBS = {
    "stock-prices": RefreshJob(
        job_id="stock-prices",
        label="S&P 500 가격/시총 갱신",
        command=[sys.executable, "-u", "-m", "pipeline_common.refresh_sp500_shared_prices"],
        description="공통 SQLite와 CSV 가격 데이터를 갱신합니다.",
    ),
    "quarterly-fundamentals": RefreshJob(
        job_id="quarterly-fundamentals",
        label="분기 재무 갱신",
        command=[sys.executable, "-u", "-m", "pipeline_common.refresh_shared_quarterly_fundamentals"],
        description="공통 분기 재무 데이터를 갱신합니다.",
    ),
    "stock-news": RefreshJob(
        job_id="stock-news",
        label="뉴스 데이터 갱신",
        command=[sys.executable, "-u", "-m", "pipeline_common.refresh_sp500_news"],
        description="S&P 500 뉴스 데이터를 갱신합니다.",
    ),
}

for _job in portfolio_web._refresh_job_defs():
    JOBS.setdefault(
        str(_job["job_id"]),
        RefreshJob(
            job_id=str(_job["job_id"]),
            label=str(_job.get("label", _job["job_id"])),
            command=portfolio_web._refresh_subprocess_command(str(_job["job_id"])),
            description=str(_job.get("description", "")),
        ),
    )

states: dict[str, JobState] = {job_id: JobState() for job_id in JOBS}
lock = threading.Lock()


def list_jobs() -> list[dict[str, object]]:
    with lock:
        return [
            {
                "job_id": job.job_id,
                "label": job.label,
                "description": job.description,
                "status": states[job.job_id].status,
                "run_id": states[job.job_id].run_id,
                "started_at": states[job.job_id].started_at,
                "finished_at": states[job.job_id].finished_at,
                "error": states[job.job_id].error,
                "logs": states[job.job_id].logs[-80:],
            }
            for job in JOBS.values()
        ]


def _append(job_id: str, line: str) -> None:
    with lock:
        state = states[job_id]
        state.logs.append(str(line).rstrip())
        state.logs = state.logs[-300:]


def _run_job(job: RefreshJob) -> None:
    root = settings.project_root
    try:
        _append(job.job_id, f"[{job.job_id}] started: {' '.join(job.command)}")
        proc = subprocess.Popen(
            job.command,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        if proc.stdout is not None:
            for line in proc.stdout:
                if line.strip():
                    _append(job.job_id, line)
        exit_code = int(proc.wait())
        with lock:
            state = states[job.job_id]
            state.status = "completed" if exit_code == 0 else f"failed({exit_code})"
            state.error = None if exit_code == 0 else f"exit_code={exit_code}"
            state.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _append(job.job_id, f"[{job.job_id}] finished with exit_code={exit_code}")
    except Exception as exc:
        with lock:
            state = states[job.job_id]
            state.status = "failed"
            state.error = f"{type(exc).__name__}: {exc}"
            state.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _append(job.job_id, f"[{job.job_id}] failed: {type(exc).__name__}: {exc}")


def start_job(job_id: str) -> dict[str, object]:
    job = JOBS.get(job_id)
    if job is None:
        return {"ok": False, "error": "unknown job"}
    with lock:
        if any(state.status == "running" for state in states.values()):
            return {"ok": False, "error": "another refresh job is already running"}
        state = states[job_id]
        state.status = "running"
        state.run_id += 1
        state.started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state.finished_at = None
        state.error = None
        state.logs = []
    thread = threading.Thread(target=_run_job, args=(job,), daemon=True)
    thread.start()
    return {"ok": True, "job_id": job_id}


def start_original_job(job_id: str) -> dict[str, object]:
    return start_job(job_id)


def original_status_payload() -> dict[str, object]:
    jobs = []
    for original in portfolio_web._refresh_job_defs():
        job_id = str(original["job_id"])
        state = states.setdefault(job_id, JobState())
        jobs.append(
            {
                "job_id": job_id,
                "label": original.get("label", job_id),
                "button_label": original.get("button_label", "Run"),
                "status": state.status,
                "run_id": state.run_id,
                "started_at": state.started_at,
                "finished_at": state.finished_at,
                "logs": state.logs[-100:],
                "log_count": len(state.logs),
                "updated_items": [],
                "latest_summary": state.error or state.status,
                "latest_items": [],
            }
        )
    return {
        "running": any(state.status == "running" for state in states.values()),
        "current_job_id": next((job_id for job_id, state in states.items() if state.status == "running"), None),
        "jobs": jobs,
    }


def render_original_refresh_page(
    *,
    lookback_days: int,
    start_date: str | None,
    end_date: str | None,
) -> str:
    from app.services.portfolio_service import resolve_range

    date_range = resolve_range(start_date, end_date, lookback_days)
    return portfolio_web._refresh_page(
        portfolio_web._PageContext(
            dashboard=None,
            lookback_days=date_range.lookback_days,
            start_date=date_range.start_date,
            end_date=date_range.end_date,
        )
    )


def refresh_page() -> str:
    rows = []
    for job in list_jobs():
        job_id = str(job["job_id"])
        rows.append(
            "<tr>"
            f"<td>{job['label']}</td>"
            f"<td>{job['status']}</td>"
            f"<td>{job['started_at'] or '-'}</td>"
            f"<td>{job['finished_at'] or '-'}</td>"
            "<td>"
            f"<form method='post' action='/refresh/{job_id}/run'>"
            "<button class='service-button' type='submit'>실행</button>"
            "</form>"
            "</td>"
            "</tr>"
        )
    return f"""
    <div class="service-stack">
      <div class="service-card">
        <h1>데이터 갱신</h1>
        <p class="service-muted">가격, 분기 재무, 뉴스 갱신을 단일 서비스에서 순차 실행합니다.</p>
      </div>
      <div class="service-card">
        <table class="service-table">
          <thead><tr><th>작업</th><th>상태</th><th>시작</th><th>종료</th><th>실행</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>
      <div class="service-card">
        <p><a href="/api/refresh/jobs">JSON 상태 보기</a></p>
      </div>
    </div>
    """
