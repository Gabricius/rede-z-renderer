"""
Thin async wrappers around the `rclone` CLI.

The legacy bash kicked off `rclone copy ... &` calls in parallel and `wait`-ed
on PIDs. We reproduce that pattern with asyncio.gather so the n8n flow keeps
exactly the same download semantics — but now from Python, with structured
error handling.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class RcloneResult:
    folder_id: str
    dest: Path
    returncode: int
    stderr: str


async def _run_rclone(args: List[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def _remote() -> str:
    return os.environ.get("RCLONE_REMOTE", "gdrive")


async def download_folder(folder_id: str, dest: Path,
                          transfers: int = 8) -> RcloneResult:
    """
    `rclone copy --drive-root-folder-id <id> <remote>: <dest> -P --transfers <N>`

    Mirrors the legacy bash one-liner. Uses Drive root-folder-id so the n8n side
    can pass any folder ID without needing a pre-configured remote per folder.
    """
    dest.mkdir(parents=True, exist_ok=True)
    args = [
        "rclone", "copy",
        "--drive-root-folder-id", folder_id,
        f"{_remote()}:",
        str(dest),
        "--transfers", str(transfers),
        "--checkers", "8",
        "--fast-list",
        "--retries", "3",
        "--low-level-retries", "10",
        "--stats=0",                            # quiet; we tail the log if needed
    ]
    log.info("rclone download: folder=%s -> %s", folder_id, dest)
    rc, _, stderr = await _run_rclone(args)
    return RcloneResult(folder_id=folder_id, dest=dest, returncode=rc, stderr=stderr)


async def download_all(folders: Dict[str, str], workdir: Path) -> List[RcloneResult]:
    """
    Download every folder concurrently. `folders` maps subdir name -> Drive folder id.
    Returns one RcloneResult per folder.
    """
    tasks = [
        download_folder(folder_id, workdir / subdir)
        for subdir, folder_id in folders.items()
    ]
    results = await asyncio.gather(*tasks)
    failed = [r for r in results if r.returncode != 0]
    if failed:
        details = "\n".join(f"  {r.folder_id} (rc={r.returncode}): {r.stderr[:300]}"
                            for r in failed)
        raise RuntimeError(f"rclone download failures:\n{details}")
    return list(results)


async def upload_file(local_path: Path, folder_id: str,
                      remote_filename: Optional[str] = None) -> str:
    """
    Upload a single file to a Drive folder. Returns the Drive file ID.

    Strategy: `rclone copyto remote: <local> --drive-root-folder-id <id>`
    then list to grab the ID.
    """
    if not local_path.exists():
        raise FileNotFoundError(f"upload source missing: {local_path}")

    dest_name = remote_filename or local_path.name
    args_copy = [
        "rclone", "copyto",
        "--drive-root-folder-id", folder_id,
        str(local_path),
        f"{_remote()}:{dest_name}",
        "--transfers", "4",
        "--retries", "3",
        "--low-level-retries", "10",
    ]
    log.info("rclone upload: %s -> drive folder=%s/%s", local_path, folder_id, dest_name)
    rc, _, stderr = await _run_rclone(args_copy)
    if rc != 0:
        raise RuntimeError(f"rclone upload failed (rc={rc}): {stderr[:500]}")

    # Resolve Drive ID via lsf --format i.
    args_lsf = [
        "rclone", "lsf",
        "--drive-root-folder-id", folder_id,
        f"{_remote()}:",
        "--format", "ip",
        "--files-only",
    ]
    rc, stdout, stderr = await _run_rclone(args_lsf)
    if rc != 0:
        raise RuntimeError(f"rclone lsf failed (rc={rc}): {stderr[:500]}")
    for line in stdout.splitlines():
        parts = line.split(";", 1)
        if len(parts) == 2 and parts[1] == dest_name:
            return parts[0]
    raise RuntimeError(f"uploaded file '{dest_name}' not found in folder listing")


def concat_audio_demux(audio_files: List[Path], output: Path) -> None:
    """
    Concatenate audio files via ffmpeg concat demuxer with `-c copy` — no re-encode.
    Synchronous because this is fast (just a remux).
    """
    if not audio_files:
        raise ValueError("audio_files is empty")
    list_file = output.with_suffix(".concat.txt")
    list_file.write_text(
        "\n".join(f"file {shlex.quote(str(p))}" for p in audio_files),
        encoding="utf-8",
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        str(output),
    ]
    log.info("ffmpeg audio concat (-c copy): %d files -> %s", len(audio_files), output)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"audio concat failed: {res.stderr[:500]}")


def ffprobe_duration(path: Path) -> float:
    """Return media duration in seconds via ffprobe."""
    res = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {res.stderr[:300]}")
    return float(res.stdout.strip())
