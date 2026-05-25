"""
Render pipelines — bash passthrough (Phase 1) + structured (Phase 2+).

Each writes JobStatus to /tmp/rede-z/<jobId>/status.json so GET /status
returns fresh data mid-render.

Phase 1 (bash) also emits a periodic progress line to stdout (which Easypanel
shows in the slot's log panel), and updates status.json with phase + pct.
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
# Progress tracker — parses the bash output stream and derives % + phase
# ----------------------------------------------------------------------------

class ProgressTracker:
    """
    Reads bytes streamed from the bash subprocess and extracts:
      - the current high-level phase (rclone, drone normalize, ffmpeg unified, ...)
      - the audio duration (so we can compute % of the final pass)
      - the most recent ffmpeg `time=HH:MM:SS.ms` reported during the final
        encode (the long one — typically 60-80% of total wall time)
      - the most recent ffmpeg `speed=X.XXx`
    """

    PHASE_PATTERNS: list[tuple[str, re.Pattern]] = [
        ("Download Drive (rclone)",      re.compile(r"INICIANDO: Download de arquivos do Drive")),
        ("Lendo configurações",          re.compile(r"INICIANDO: Leitura de configura")),
        ("Concat áudios",                re.compile(r"INICIANDO: Concatenação de áudios")),
        ("Selecionando drones",          re.compile(r"FLUXO: DRONE")),
        ("Normalizando drones",          re.compile(r"Normalizando vídeos drone")),
        ("Efeitos visuais (Drone)",      re.compile(r"Aplicando efeitos visuais do Drone")),
        ("Intro entrevista (V2.1)",      re.compile(r"V2\.1: Pré-pendendo intro")),
        ("Processando IMAGEM/VÍDEO",     re.compile(r"FLUXO: IMAGEM/VÍDEO")),
        ("Gerando zoom das imagens",     re.compile(r"PASSO 1: Gerando vídeos com ZOOM")),
        ("Processando visual",           re.compile(r"PASSO 2: Processando o VISUAL")),
        ("Montando sequência",           re.compile(r"PASSO 3: Montando sequência")),
        ("Concatenando sequência",       re.compile(r"PASSO 4: Concatenando")),
        ("Processamento UNIFICADO",      re.compile(r"Executando processamento UNIFICADO")),
        ("Concluído (bash)",             re.compile(r"=== Vídeo finalizado com sucesso")),
    ]

    DURATION_RE = re.compile(r"Duração total:\s*([\d.]+)\s*segundos")
    FFMPEG_TIME_RE = re.compile(r"time=(\d+):(\d+):([\d.]+)")
    SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")

    def __init__(self) -> None:
        self.audio_duration: Optional[float] = None
        self.current_phase: str = "iniciando"
        self.phase_started_at: float = time.time()
        self.last_speed: Optional[float] = None
        self.last_time_seconds: Optional[float] = None
        self.bytes_seen: int = 0

    def feed(self, chunk: bytes) -> None:
        self.bytes_seen += len(chunk)
        try:
            text = chunk.decode("utf-8", errors="replace")
        except Exception:
            return

        if self.audio_duration is None:
            m = self.DURATION_RE.search(text)
            if m:
                try:
                    self.audio_duration = float(m.group(1))
                except ValueError:
                    pass

        for name, pat in self.PHASE_PATTERNS:
            if pat.search(text):
                if self.current_phase != name:
                    self.current_phase = name
                    self.phase_started_at = time.time()
                break

        for m in self.SPEED_RE.finditer(text):
            try:
                self.last_speed = float(m.group(1))
            except ValueError:
                pass

        for m in self.FFMPEG_TIME_RE.finditer(text):
            try:
                h, mn, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                self.last_time_seconds = h * 3600 + mn * 60 + s
            except ValueError:
                pass

    # ------- accessors -------

    def pct_of_final_pass(self) -> Optional[float]:
        if self.audio_duration and self.last_time_seconds is not None:
            return min(100.0, 100.0 * self.last_time_seconds / self.audio_duration)
        return None

    def eta_seconds(self) -> Optional[float]:
        if (
            self.last_speed and self.last_speed > 0
            and self.audio_duration is not None
            and self.last_time_seconds is not None
        ):
            remaining_video = max(0.0, self.audio_duration - self.last_time_seconds)
            return remaining_video / self.last_speed
        return None

    def summary_line(self, job_id: str, started_at: float) -> str:
        parts = [f"[{job_id}] FASE: {self.current_phase}"]
        pct = self.pct_of_final_pass()
        if pct is not None:
            parts.append(f"progresso: {pct:0.1f}%")
        if self.last_speed is not None:
            parts.append(f"speed: {self.last_speed:0.2f}x")
        eta = self.eta_seconds()
        if eta is not None:
            parts.append(f"ETA: {_fmt_hms(eta)}")
        parts.append(f"decorrido: {_fmt_hms(time.time() - started_at)}")
        return " | ".join(parts)


def _fmt_hms(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h {m:02d}min"
    if m > 0:
        return f"{m}min {s:02d}s"
    return f"{s}s"


# ----------------------------------------------------------------------------
# Subprocess runner with progress reporting
# ----------------------------------------------------------------------------

async def _exec_with_progress(
    argv: list[str],
    log_path: Path,
    nice_level: int,
    progress: ProgressTracker,
    job_id: str,
    workdir: Path,
    status: JobStatus,
    started_at: float,
    progress_interval_sec: float = 10.0,
) -> int:
    """
    Run the subprocess; stream stdout/stderr to log_path; every N seconds emit
    a one-line progress summary to stdout (Easypanel shows it) and update
    status.json with the current phase + pct + speed.
    """
    if os.name == "posix" and nice_level > 0:
        argv = ["nice", "-n", str(nice_level)] + argv

    last_report = 0.0

    with log_path.open("wb") as logf:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None

        while True:
            chunk = await proc.stdout.read(8192)
            if not chunk:
                break
            logf.write(chunk)
            logf.flush()
            progress.feed(chunk)

            now = time.time()
            if now - last_report >= progress_interval_sec:
                last_report = now
                line = progress.summary_line(job_id, started_at)
                # Print to stdout so Easypanel "Logs" panel shows the progress.
                print(line, flush=True)
                # Also update status.json so /status endpoint stays fresh.
                status.ffmpegSpeed = progress.last_speed
                pct = progress.pct_of_final_pass()
                status.logTail = line
                # Inline the phase + pct into errorMsg-less metadata (we
                # piggyback on logTail to keep the schema small).
                write_status(workdir, status)

        rc = await proc.wait()

        # Final progress line (so the Easypanel log has the closing summary).
        print(progress.summary_line(job_id, started_at) + f" | bash rc={rc}", flush=True)

    return rc


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

    started_at = time.time()
    status = JobStatus(
        jobId=cfg.jobId,
        mode="bash",
        status="queued",
        pid=os.getpid(),
        startedAt=started_at,
        meta=cfg.meta,                          # echoed back to n8n in the callback
    )
    write_status(workdir, status)

    nice_level = int(os.environ.get("FFMPEG_NICE", "10"))
    keep_tmp = os.environ.get("KEEP_TMP_ON_SUCCESS", "false").lower() == "true"

    bash_path = workdir / "render.sh"
    bash_log = workdir / "render.log"
    progress = ProgressTracker()

    print(f"[{cfg.jobId}] dispatched — workdir={workdir} | nice={nice_level}", flush=True)

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

        rc = await _exec_with_progress(
            argv=["bash", str(bash_path)],
            log_path=bash_log,
            nice_level=nice_level,
            progress=progress,
            job_id=cfg.jobId,
            workdir=workdir,
            status=status,
            started_at=started_at,
        )
        status.ffmpegSpeed = progress.last_speed
        if rc != 0:
            raise RuntimeError(f"bash exited with rc={rc}. See render.log.")

        # 4. The bash leaves /tmp/<jobId>/final.mp4 (not workdir/final.mp4).
        final_path = Path("/tmp") / cfg.jobId / cfg.outputFilename
        if not final_path.exists():
            raise FileNotFoundError(f"bash succeeded but final not found at {final_path}")

        # 5. Upload to Drive.
        status.status = "uploading"
        write_status(workdir, status)
        print(f"[{cfg.jobId}] FASE: Upload final.mp4 para Drive", flush=True)
        drive_id = await _rclone_upload(final_path, cfg.outputDriveFolderId, cfg.outputFilename)

        # 6. Finalize.
        status.status = "ok"
        status.finishedAt = time.time()
        status.durationSec = status.finishedAt - started_at
        status.finalDriveId = drive_id
        status.logTail = _tail(bash_log, 40)
        write_status(workdir, status)
        print(
            f"[{cfg.jobId}] OK — driveId={drive_id} | total={_fmt_hms(status.durationSec)}",
            flush=True,
        )

    except Exception as e:
        log.exception("[%s] bash pipeline failed", cfg.jobId)
        status.status = "error"
        status.finishedAt = time.time()
        status.durationSec = status.finishedAt - started_at
        status.errorMsg = str(e)[:2000]
        status.logTail = _tail(bash_log, 80)
        write_status(workdir, status)
        print(f"[{cfg.jobId}] ERRO — {status.errorMsg}", flush=True)

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
