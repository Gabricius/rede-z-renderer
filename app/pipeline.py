"""
Render pipelines — bash passthrough (Phase 1) + structured (Phase 2+).

Each writes JobStatus to /tmp/rede-z/<jobId>/status.json so GET /status
returns fresh data mid-render.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from app.config_schema import (
    BashRenderConfig,
    JobStatus,
    StructuredRenderConfig,
)
from app.callback import notify

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Status helpers
# ----------------------------------------------------------------------------

def _status_path(workdir: Path) -> Path:
    return workdir / "status.json"


def write_status(workdir: Path, status: JobStatus) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    _status_path(workdir).write_text(status.model_dump_json(indent=2), encoding="utf-8")


def read_status(workdir: Path) -> Optional[JobStatus]:
    path = _status_path(workdir)
    if not path.exists():
        return None
    try:
        return JobStatus.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("could not parse status.json: %s", e)
        return None


# ----------------------------------------------------------------------------
# Subprocess runner — streams output, captures last ffmpeg speed
# ----------------------------------------------------------------------------

_SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")


async def _exec(argv: list[str], log_path: Path, nice_level: int,
                cwd: Optional[Path] = None) -> tuple[int, Optional[float]]:
    if os.name == "posix" and nice_level > 0:
        argv = ["nice", "-n", str(nice_level)] + argv

    last_speed: Optional[float] = None
    with log_path.open("wb") as logf:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd) if cwd else None,
        )
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(8192)
            if not chunk:
                break
            logf.write(chunk)
            logf.flush()
            try:
                txt = chunk.decode("utf-8", errors="replace")
                m = _SPEED_RE.search(txt)
                if m:
                    last_speed = float(m.group(1))
            except Exception:
                pass
        rc = await proc.wait()
        return rc, last_speed


def _tail(path: Path, lines: int) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as f:
        try:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = min(size, 128 * 1024)
            f.seek(size - block, os.SEEK_SET)
            data = f.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
    return "\n".join(data.splitlines()[-lines:])


# ----------------------------------------------------------------------------
# Rclone upload (used by bash-passthrough mode)
# ----------------------------------------------------------------------------

async def _rclone_upload(local_path: Path, folder_id: str, remote_filename: str) -> str:
    """Upload to Drive and return the file id. Uses RCLONE_REMOTE env (default 'gdrive')."""
    if not local_path.exists():
        raise FileNotFoundError(f"upload source missing: {local_path}")

    remote = os.environ.get("RCLONE_REMOTE", "gdrive")

    args_copy = [
        "rclone", "copyto",
        "--drive-root-folder-id", folder_id,
        str(local_path),
        f"{remote}:{remote_filename}",
        "--transfers", "4",
        "--retries", "3",
        "--low-level-retries", "10",
    ]
    log.info("rclone upload: %s -> %s/%s", local_path, folder_id, remote_filename)
    proc = await asyncio.create_subprocess_exec(
        *args_copy, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"rclone upload failed (rc={proc.returncode}): "
            f"{stderr.decode(errors='replace')[:500]}"
        )

    args_lsf = [
        "rclone", "lsf",
        "--drive-root-folder-id", folder_id,
        f"{remote}:",
        "--format", "ip",
        "--files-only",
    ]
    proc = await asyncio.create_subprocess_exec(
        *args_lsf, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"rclone lsf failed: {stderr.decode(errors='replace')[:500]}")
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split(";", 1)
        if len(parts) == 2 and parts[1] == remote_filename:
            return parts[0]
    raise RuntimeError(f"uploaded file '{remote_filename}' not found in folder listing")


# ============================================================================
# MODE 1 — Bash passthrough
# ============================================================================

async def run_bash_pipeline(cfg: BashRenderConfig, base_dir: Path) -> JobStatus:
    """
    Write the bash to disk, execute it, then upload the produced final.mp4.

    The bash itself does:  cd /tmp ; mkdir -p $jobId ; cd $jobId ; rclone ... ; ffmpeg ...
    leaving /tmp/<jobId>/final.mp4 when successful.
    """
    workdir = base_dir / cfg.jobId
    workdir.mkdir(parents=True, exist_ok=True)

    status = JobStatus(
        jobId=cfg.jobId,
        mode="bash",
        status="queued",
        pid=os.getpid(),
        startedAt=time.time(),
    )
    write_status(workdir, status)

    nice_level = int(os.environ.get("FFMPEG_NICE", "10"))
    keep_tmp = os.environ.get("KEEP_TMP_ON_SUCCESS", "false").lower() == "true"

    bash_path = workdir / "render.sh"
    bash_log = workdir / "render.log"

    try:
        # 1. Save the bash to a file (preserves exactly what the JS produced).
        bash_path.write_text(cfg.bashScript, encoding="utf-8")
        os.chmod(bash_path, 0o755)

        # 2. Save a snapshot of meta for debugging.
        meta_path = workdir / "meta.json"
        meta_path.write_text(_json.dumps({
            "jobId": cfg.jobId,
            "outputDriveFolderId": cfg.outputDriveFolderId,
            "outputFilename": cfg.outputFilename,
            "callbackUrl": str(cfg.callbackUrl),
            "meta": cfg.meta,
        }, indent=2), encoding="utf-8")

        # 3. Run the bash. It cd's into /tmp/<jobId> itself.
        status.status = "rendering"
        write_status(workdir, status)
        t0 = time.time()
        rc, last_speed = await _exec(["bash", str(bash_path)], bash_log, nice_level)
        render_seconds = time.time() - t0
        status.ffmpegSpeed = last_speed
        log.info("[%s] bash finished rc=%d in %.1fs (last speed=%s)",
                 cfg.jobId, rc, render_seconds, last_speed)
        if rc != 0:
            raise RuntimeError(f"bash exited with rc={rc}. See render.log.")

        # 4. The bash leaves /tmp/<jobId>/final.mp4 (not workdir/final.mp4).
        final_path = Path("/tmp") / cfg.jobId / cfg.outputFilename
        if not final_path.exists():
            raise FileNotFoundError(f"bash succeeded but final not found at {final_path}")

        # 5. Upload to Drive.
        status.status = "uploading"
        write_status(workdir, status)
        drive_id = await _rclone_upload(final_path, cfg.outputDriveFolderId, cfg.outputFilename)

        # 6. Finalize.
        status.status = "ok"
        status.finishedAt = time.time()
        status.durationSec = status.finishedAt - (status.startedAt or status.finishedAt)
        status.finalDriveId = drive_id
        status.logTail = _tail(bash_log, 40)
        write_status(workdir, status)

    except Exception as e:
        log.exception("[%s] bash pipeline failed", cfg.jobId)
        status.status = "error"
        status.finishedAt = time.time()
        status.durationSec = status.finishedAt - (status.startedAt or status.finishedAt)
        status.errorMsg = str(e)[:2000]
        status.logTail = _tail(bash_log, 80)
        write_status(workdir, status)

    # Callback (fire even on error).
    try:
        await notify(str(cfg.callbackUrl), status, token=cfg.callbackToken)
    except Exception:
        log.exception("[%s] callback failed", cfg.jobId)

    # Cleanup the /tmp/<jobId>/ tree on success unless asked to keep.
    if status.status == "ok" and not keep_tmp:
        legacy_tmp = Path("/tmp") / cfg.jobId
        shutil.rmtree(legacy_tmp, ignore_errors=True)

    return status


# ============================================================================
# MODE 2 — Structured (Phase 2+, placeholder — implementation deferred)
# ============================================================================

async def run_structured_pipeline(cfg: StructuredRenderConfig, base_dir: Path) -> JobStatus:
    """Reserved for Phase 2+. For now, returns an explicit error status."""
    workdir = base_dir / cfg.jobId
    status = JobStatus(
        jobId=cfg.jobId,
        mode="structured",
        status="error",
        startedAt=time.time(),
        finishedAt=time.time(),
        errorMsg="structured mode not yet implemented; use bash passthrough (send `bashScript`).",
    )
    write_status(workdir, status)
    try:
        await notify(str(cfg.callbackUrl), status, token=cfg.callbackToken)
    except Exception:
        log.exception("[%s] callback failed", cfg.jobId)
    return status
