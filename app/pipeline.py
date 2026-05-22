"""
End-to-end orchestration for a single render job.

Steps (each writes JobStatus to disk so /status/:jobId stays current):
  1. download         — rclone copy all folders concurrently
  2. concat_audio     — ffmpeg concat demuxer (-c copy) of audio files
  3. ffprobe          — discover total duration
  4. write_srt        — emit SRT file from cfg.srtBlocks (if any)
  5. render           — single-pass ffmpeg with consolidated filter_complex
  6. upload           — rclone upload final.mp4 to Drive
  7. callback         — POST status to n8n
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from app.config_schema import JobStatus, RenderConfig
from app.callback import notify
from app.ffmpeg_builder import build_ffmpeg_command, write_srt_file
from app.rclone_client import (
    concat_audio_demux,
    download_all,
    ffprobe_duration,
    upload_file,
)

log = logging.getLogger(__name__)


# ---------- Status file helpers ----------

def _status_path(workdir: Path) -> Path:
    return workdir / "status.json"


def write_status(workdir: Path, status: JobStatus) -> None:
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


# ---------- ffmpeg execution ----------

_SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")


async def _run_ffmpeg(argv: list[str], log_path: Path,
                      nice_level: int) -> tuple[int, Optional[float]]:
    """
    Run ffmpeg, streaming stderr to a log file. Captures the last `speed=X.XXx`
    value reported for the comparative report.

    `nice_level` is applied via the `nice` wrapper on Linux. On other OSes the
    nice prefix is silently dropped.
    """
    if os.name == "posix" and nice_level > 0:
        argv = ["nice", "-n", str(nice_level)] + argv

    with log_path.open("wb") as logf:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        last_speed: Optional[float] = None
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            logf.write(chunk)
            logf.flush()
            try:
                text = chunk.decode("utf-8", errors="replace")
                m = _SPEED_RE.search(text)
                if m:
                    last_speed = float(m.group(1))
            except Exception:
                pass

        rc = await proc.wait()
        return rc, last_speed


# ---------- Main pipeline ----------

async def run_pipeline(cfg: RenderConfig, base_dir: Path) -> JobStatus:
    """
    Execute the full render pipeline. Always returns a JobStatus (ok or error),
    never raises — callers can trust the return value.
    """
    workdir = base_dir / cfg.jobId
    workdir.mkdir(parents=True, exist_ok=True)

    status = JobStatus(
        jobId=cfg.jobId,
        status="queued",
        pid=os.getpid(),
        startedAt=time.time(),
    )
    write_status(workdir, status)

    nice_level = int(os.environ.get("FFMPEG_NICE", "10"))
    max_threads = int(os.environ.get("MAX_FFMPEG_THREADS", "0"))
    keep_tmp = os.environ.get("KEEP_TMP_ON_SUCCESS", "false").lower() == "true"

    try:
        # 1. Download all folders in parallel.
        status.status = "downloading"
        write_status(workdir, status)
        await download_all(
            {
                "audios": cfg.folders.audios,
                "visuais": cfg.folders.visuais,
                "drones": cfg.folders.drones,
                "overlays": cfg.folders.overlays,
            },
            workdir,
        )

        # 2. Concat audio (-c copy demuxer — no re-encode).
        audio_paths = [workdir / "audios" / name for name in cfg.audioFiles]
        missing = [p for p in audio_paths if not p.exists()]
        if missing:
            raise FileNotFoundError(f"missing audio inputs: {missing}")
        full_audio = workdir / "full_audio.mp3"
        concat_audio_demux(audio_paths, full_audio)

        # 3. Probe duration (debug / report only).
        audio_duration = ffprobe_duration(full_audio)
        log.info("[%s] full_audio duration=%.2fs", cfg.jobId, audio_duration)

        # 4. Write SRT.
        srt_path = workdir / "subtitles.srt"
        if cfg.srtBlocks:
            write_srt_file(cfg.srtBlocks, srt_path)

        # 5. Build + run the consolidated ffmpeg command.
        built = build_ffmpeg_command(
            cfg=cfg,
            workdir=workdir,
            full_audio_path=full_audio,
            srt_path=srt_path,
            max_threads=max_threads,
        )
        (workdir / "ffmpeg_command.sh").write_text(
            "#!/usr/bin/env bash\n# Reproducible ffmpeg invocation for this job.\nset -euo pipefail\n\n"
            + " \\\n  ".join(map(_shell_quote, built.argv))
            + "\n",
            encoding="utf-8",
        )

        status.status = "rendering"
        write_status(workdir, status)

        ffmpeg_log = workdir / "ffmpeg.log"
        t0 = time.time()
        rc, last_speed = await _run_ffmpeg(built.argv, ffmpeg_log, nice_level)
        render_seconds = time.time() - t0
        status.ffmpegSpeed = last_speed
        if rc != 0:
            tail = _tail(ffmpeg_log, 80)
            raise RuntimeError(f"ffmpeg failed (rc={rc}). Tail:\n{tail}")
        log.info("[%s] ffmpeg finished in %.1fs (last speed=%sx)",
                 cfg.jobId, render_seconds, last_speed)

        # 6. Upload final.mp4.
        status.status = "uploading"
        write_status(workdir, status)
        drive_id = await upload_file(
            built.output_path,
            cfg.folders.output,
            remote_filename=cfg.output.filename,
        )

        # 7. Finalize status.
        status.status = "ok"
        status.finishedAt = time.time()
        status.durationSec = status.finishedAt - (status.startedAt or status.finishedAt)
        status.finalDriveId = drive_id
        status.logTail = _tail(ffmpeg_log, 40)
        write_status(workdir, status)

    except Exception as e:
        log.exception("[%s] pipeline failed", cfg.jobId)
        status.status = "error"
        status.finishedAt = time.time()
        status.durationSec = status.finishedAt - (status.startedAt or status.finishedAt)
        status.errorMsg = str(e)[:2000]
        ffmpeg_log = workdir / "ffmpeg.log"
        if ffmpeg_log.exists():
            status.logTail = _tail(ffmpeg_log, 80)
        write_status(workdir, status)

    # Callback (fire even on error).
    try:
        await notify(str(cfg.callbackUrl), status, token=cfg.callbackToken)
    except Exception:
        log.exception("[%s] callback failed", cfg.jobId)

    # Cleanup tmp on success unless asked to keep.
    if status.status == "ok" and not keep_tmp:
        for sub in ("audios", "visuais", "drones", "overlays"):
            target = workdir / sub
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)

    return status


# ---------- helpers ----------

def _tail(path: Path, lines: int) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as f:
        try:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = min(size, 64 * 1024)
            f.seek(size - block, os.SEEK_SET)
            data = f.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
    return "\n".join(data.splitlines()[-lines:])


def _shell_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)
