"""
Pydantic schema for the JSON config that n8n posts to POST /render.

The n8n side (`Monta código FFmpeg` node) keeps the creative decisions:
  - which drones/intros to pick,
  - which images to use,
  - which audio bitrate / CRF to randomize,
  - SRT block timings,
  - destination Drive folder IDs.

This service receives those decisions as a single JSON document and
executes the render. No business logic lives here — only execution.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, HttpUrl


# ---------- Inputs ----------

class Folders(BaseModel):
    """Google Drive folder IDs that the rclone client downloads into local
    subdirectories of the job working folder."""
    audios: str
    visuais: str
    drones: str
    overlays: str
    output: str                              # where final.mp4 is uploaded


class DroneClip(BaseModel):
    """A drone/intro video clip. Boomerang doubles its duration via reverse-concat."""
    source: str = Field(..., description="Filename relative to the drones folder")
    boomerang: bool = True
    duration: float = Field(..., gt=0, description="Final timeline duration (seconds)")


class VisualClip(BaseModel):
    """An image or short video that occupies a slot in the timeline."""
    source: str = Field(..., description="Filename relative to the visuais folder")
    kind: Literal["image", "video"] = "image"
    duration: float = Field(..., gt=0, description="Timeline duration (seconds)")
    effect: Literal[
        "zoompan_in", "zoompan_out", "flip_h", "flip_v", "static"
    ] = "zoompan_in"


class SrtBlock(BaseModel):
    start: float = Field(..., ge=0, description="Start time in seconds")
    end: float = Field(..., gt=0, description="End time in seconds")
    text: str


class OverlayRec(BaseModel):
    """The green-screen REC indicator overlay."""
    source: str = Field(..., description="Filename relative to the overlays folder")
    # Anchor (x, y) in pixels from top-left of the 1920x1080 frame.
    x: int = 1700
    y: int = 60


# ---------- Anti-fingerprint ----------

class AntiFingerprint(BaseModel):
    """Pre-rolled by n8n so each run is unique. Keeping randomization on the JS
    side keeps it inspectable by the operator."""
    crf_final: int = Field(..., ge=18, le=28, description="Random within [21,23] per spec")
    audio_bitrate: str = Field(..., description='One of "160k" | "192k" | "224k"')


# ---------- Output config ----------

class Output(BaseModel):
    width: int = 1920
    height: int = 1080
    fps: int = 30
    pix_fmt: str = "yuv420p"
    container: Literal["mp4"] = "mp4"
    filename: str = "final.mp4"


# ---------- Top-level render request ----------

class RenderConfig(BaseModel):
    jobId: str = Field(..., min_length=1, description="Trello card ID or any unique slug")

    folders: Folders

    audioFiles: List[str] = Field(
        ..., min_length=1,
        description="Ordered list of audio filenames (relative to audios folder) to concat with -c copy"
    )

    drones: List[DroneClip] = Field(default_factory=list)
    visuals: List[VisualClip] = Field(..., min_length=1)
    overlayRec: Optional[OverlayRec] = None
    srtBlocks: List[SrtBlock] = Field(default_factory=list)

    fontPath: str = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    antiFingerprint: AntiFingerprint
    output: Output = Field(default_factory=Output)

    callbackUrl: HttpUrl = Field(
        ..., description="n8n webhook URL that receives the completion POST"
    )
    callbackToken: Optional[str] = Field(
        default=None,
        description="Echoed back in the X-Render-Token header. If omitted, uses CALLBACK_TOKEN env."
    )


# ---------- Status (written to disk and returned by GET /status/:jobId) ----------

class JobStatus(BaseModel):
    jobId: str
    status: Literal["queued", "downloading", "rendering", "uploading", "ok", "error"]
    pid: Optional[int] = None
    startedAt: Optional[float] = None
    finishedAt: Optional[float] = None
    durationSec: Optional[float] = None
    ffmpegSpeed: Optional[float] = None
    finalDriveId: Optional[str] = None
    errorMsg: Optional[str] = None
    logTail: Optional[str] = None
