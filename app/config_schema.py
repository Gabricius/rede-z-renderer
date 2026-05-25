"""
Pydantic schemas for POST /render.

This service accepts TWO modes:

  1. BASH MODE (Phase 1, in use today)
     The n8n side ships the full bash script. The Python service saves it,
     executes in background, uploads /tmp/<jobId>/final.mp4 to Drive, and
     calls back the n8n webhook. No FFmpeg consolidation; the wins are
     async dispatch + container isolation.

  2. STRUCTURED MODE (Phase 2+, reserved for future)
     The n8n side decomposes the render into typed fields. The Python service
     builds a single-pass FFmpeg with -filter_complex. This is where the real
     CPU speedup comes from, ported incrementally per visual mode.

The router in main.py picks the mode by the presence of `bashScript`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, HttpUrl


# ============================================================================
# MODE 1 — Bash passthrough
# ============================================================================

class BashRenderConfig(BaseModel):
    """A complete bash script that the service runs in background.

    The bash is responsible for everything: download via rclone, ffmpeg,
    SRT processing, leaving /tmp/<jobId>/final.mp4 on disk. The Python
    service then uploads that file to outputDriveFolderId."""

    jobId: str = Field(..., min_length=1)
    bashScript: str = Field(..., min_length=1, description="Full bash script to execute")
    outputDriveFolderId: str = Field(..., description="Drive folder id to upload final.mp4 into")
    outputFilename: str = "final.mp4"

    callbackUrl: HttpUrl = Field(..., description="n8n webhook to POST status to")
    callbackToken: Optional[str] = Field(default=None, description="Echoed back as X-Render-Token")

    meta: Dict[str, Any] = Field(default_factory=dict, description="Diagnostic only; not used by Python")


# ============================================================================
# MODE 2 — Structured (reserved for Phase 2+)
# ============================================================================

class Folders(BaseModel):
    audios: str
    visuais: str
    drones: str
    overlays: str
    output: str


class DroneClip(BaseModel):
    source: str
    boomerang: bool = True
    duration: float = Field(..., gt=0)


class VisualClip(BaseModel):
    source: str
    kind: Literal["image", "video"] = "image"
    duration: float = Field(..., gt=0)
    effect: Literal["zoompan_in", "zoompan_out", "flip_h", "flip_v", "static"] = "zoompan_in"


class SrtBlock(BaseModel):
    start: float = Field(..., ge=0)
    end: float = Field(..., gt=0)
    text: str


class OverlayRec(BaseModel):
    source: str
    x: int = 1700
    y: int = 60


class AntiFingerprint(BaseModel):
    crf_final: int = Field(..., ge=18, le=28)
    audio_bitrate: str


class Output(BaseModel):
    width: int = 1920
    height: int = 1080
    fps: int = 30
    pix_fmt: str = "yuv420p"
    container: Literal["mp4"] = "mp4"
    filename: str = "final.mp4"


class StructuredRenderConfig(BaseModel):
    jobId: str = Field(..., min_length=1)
    folders: Folders
    audioFiles: List[str] = Field(..., min_length=1)
    drones: List[DroneClip] = Field(default_factory=list)
    visuals: List[VisualClip] = Field(..., min_length=1)
    overlayRec: Optional[OverlayRec] = None
    srtBlocks: List[SrtBlock] = Field(default_factory=list)
    fontPath: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    antiFingerprint: AntiFingerprint
    output: Output = Field(default_factory=Output)
    callbackUrl: HttpUrl
    callbackToken: Optional[str] = None


# ============================================================================
# Status (written to disk, returned by GET /status/<jobId>)
# ============================================================================

class JobStatus(BaseModel):
    jobId: str
    mode: Literal["bash", "structured"] = "bash"
    status: Literal["queued", "downloading", "rendering", "uploading", "ok", "error"]
    pid: Optional[int] = None
    startedAt: Optional[float] = None
    finishedAt: Optional[float] = None
    durationSec: Optional[float] = None
    ffmpegSpeed: Optional[float] = None
    finalDriveId: Optional[str] = None
    errorMsg: Optional[str] = None
    logTail: Optional[str] = None
    # Echoed back from cfg.meta so the n8n webhook can reach the original
    # Trello card id, character name, etc without round-tripping a lookup.
    meta: Dict[str, Any] = Field(default_factory=dict)
