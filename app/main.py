"""
FastAPI app for rede-z-renderer.

POST /render accepts either:
  - BashRenderConfig (has `bashScript`) -> bash passthrough pipeline
  - StructuredRenderConfig             -> structured pipeline (Phase 2+)
Detection is by presence of the `bashScript` key.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request, Response

from app.config_schema import (
    BashRenderConfig,
    JobStatus,
    StructuredRenderConfig,
)
from app.pipeline import (
    read_status,
    run_bash_pipeline,
    run_structured_pipeline,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("rede-z-renderer")

app = FastAPI(title="rede-z-renderer", version="0.2.0")


def _base_dir() -> Path:
    base = Path(os.environ.get("OUTPUT_TMP_DIR", "/tmp/rede-z"))
    base.mkdir(parents=True, exist_ok=True)
    return base


# Soft in-memory guard against double dispatch of the same jobId.
# Source of truth is status.json on disk.
_active: set[str] = set()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "rede-z-renderer", "version": app.version}


@app.post("/render", status_code=202)
async def render(request: Request) -> Dict[str, Any]:
    """Accept either schema. Mode is picked by `bashScript` presence."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "request body must be JSON")

    if not isinstance(payload, dict):
        raise HTTPException(400, "request body must be a JSON object")

    job_id = payload.get("jobId")
    if not job_id or not isinstance(job_id, str):
        raise HTTPException(400, "missing or invalid 'jobId'")

    base = _base_dir()
    workdir = base / job_id

    if job_id in _active:
        raise HTTPException(409, f"job {job_id} is already in progress")
    existing = read_status(workdir)
    if existing and existing.status in ("downloading", "rendering", "uploading"):
        raise HTTPException(
            409,
            f"job {job_id} has in-progress status on disk ({existing.status}); "
            "delete the workdir or use a different jobId."
        )

    # Route by payload shape.
    if "bashScript" in payload:
        try:
            cfg = BashRenderConfig.model_validate(payload)
        except Exception as e:
            raise HTTPException(422, f"BashRenderConfig validation failed: {e}")
        _active.add(job_id)
        asyncio.create_task(_safe_run_bash(cfg, base))
        log.info("dispatched bash job %s (workdir=%s)", job_id, workdir)
        return {
            "jobId": job_id,
            "mode": "bash",
            "status": "queued",
            "pid": os.getpid(),
            "workdir": str(workdir),
        }

    try:
        cfg_s = StructuredRenderConfig.model_validate(payload)
    except Exception as e:
        raise HTTPException(422, f"StructuredRenderConfig validation failed: {e}")
    _active.add(job_id)
    asyncio.create_task(_safe_run_structured(cfg_s, base))
    log.info("dispatched structured job %s (workdir=%s)", job_id, workdir)
    return {
        "jobId": job_id,
        "mode": "structured",
        "status": "queued",
        "pid": os.getpid(),
        "workdir": str(workdir),
    }


async def _safe_run_bash(cfg: BashRenderConfig, base: Path) -> None:
    try:
        await run_bash_pipeline(cfg, base)
    finally:
        _active.discard(cfg.jobId)


async def _safe_run_structured(cfg: StructuredRenderConfig, base: Path) -> None:
    try:
        await run_structured_pipeline(cfg, base)
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
def logs(job_id: str, tail: int = 200, kind: str = "render") -> Response:
    """
    Tail of the bash render log (or ffmpeg log in structured mode).
    `kind=render` (default) -> render.log, `kind=ffmpeg` -> ffmpeg.log.
    """
    filename = "render.log" if kind == "render" else "ffmpeg.log"
    log_path = _base_dir() / job_id / filename
    if not log_path.exists():
        raise HTTPException(404, f"no {filename} for jobId={job_id}")
    with log_path.open("rb") as f:
        try:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = min(size, 512 * 1024)
            f.seek(size - block, os.SEEK_SET)
            data = f.read().decode("utf-8", errors="replace")
        except OSError:
            data = ""
    text = "\n".join(data.splitlines()[-tail:])
    return Response(content=text, media_type="text/plain")
