"""
FastAPI entry point for the rede-z-renderer service.

Endpoints:
  GET  /health             — liveness probe.
  POST /render             — accept a RenderConfig and spawn the pipeline in the
                             background. Returns 202 with jobId + pid.
  GET  /status/{job_id}    — read the status.json from the job working folder.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Response

from app.config_schema import JobStatus, RenderConfig
from app.pipeline import read_status, run_pipeline

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("rede-z-renderer")

app = FastAPI(title="rede-z-renderer", version="0.1.0")


def _base_dir() -> Path:
    base = Path(os.environ.get("OUTPUT_TMP_DIR", "/tmp/rede-z"))
    base.mkdir(parents=True, exist_ok=True)
    return base


# In-memory set of jobs the current worker is actively running. This is a
# soft guard (the source of truth is status.json on disk). We refuse a 2nd
# /render for the same jobId while it's still in progress.
_active: set[str] = set()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "rede-z-renderer", "version": app.version}


@app.post("/render", status_code=202)
async def render(cfg: RenderConfig) -> dict:
    base = _base_dir()
    workdir = base / cfg.jobId

    if cfg.jobId in _active:
        raise HTTPException(409, f"job {cfg.jobId} is already in progress")

    existing = read_status(workdir)
    if existing and existing.status in ("downloading", "rendering", "uploading"):
        raise HTTPException(
            409,
            f"job {cfg.jobId} has an in-progress status on disk ({existing.status}); "
            "delete the workdir or use a different jobId."
        )

    # Spawn the pipeline as a background asyncio task so this handler returns
    # immediately. n8n sees a 202 within a few hundred ms and unblocks.
    _active.add(cfg.jobId)
    asyncio.create_task(_safe_run(cfg, base))

    log.info("dispatched job %s (workdir=%s)", cfg.jobId, workdir)
    return {
        "jobId": cfg.jobId,
        "status": "queued",
        "pid": os.getpid(),
        "workdir": str(workdir),
    }


async def _safe_run(cfg: RenderConfig, base: Path) -> None:
    try:
        await run_pipeline(cfg, base)
    finally:
        _active.discard(cfg.jobId)


@app.get("/status/{job_id}", response_model=JobStatus)
def status(job_id: str) -> JobStatus:
    workdir = _base_dir() / job_id
    s = read_status(workdir)
    if s is None:
        raise HTTPException(404, f"no status for jobId={job_id}")
    return s


@app.get("/logs/{job_id}")
def logs(job_id: str, tail: int = 200) -> Response:
    """Return the last N lines of the ffmpeg log for quick debugging."""
    log_path = _base_dir() / job_id / "ffmpeg.log"
    if not log_path.exists():
        raise HTTPException(404, f"no ffmpeg.log for jobId={job_id}")
    with log_path.open("rb") as f:
        try:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = min(size, 256 * 1024)
            f.seek(size - block, os.SEEK_SET)
            data = f.read().decode("utf-8", errors="replace")
        except OSError:
            data = ""
    text = "\n".join(data.splitlines()[-tail:])
    return Response(content=text, media_type="text/plain")
