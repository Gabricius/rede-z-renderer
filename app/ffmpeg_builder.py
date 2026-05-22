"""
Build the consolidated single-pass FFmpeg command.

The legacy bash pipeline did 4-5 cascading re-encodes:
    drone -> tmp1 (encode) -> tmp2 (encode w/ effects) -> tmp3 (xfade w/ intro)
    -> tmp4 (overlay REC) -> tmp5 (subtitles) -> final (re-encode w/ medium preset)

This module collapses all of that into ONE ffmpeg invocation with a single
-filter_complex graph. The 1-pass design is the largest single source of
speedup, ahead of any preset tuning.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from app.config_schema import (
    DroneClip,
    OverlayRec,
    RenderConfig,
    SrtBlock,
    VisualClip,
)


# ---------- SRT helpers ----------

def _srt_timecode(seconds: float) -> str:
    """Format seconds as HH:MM:SS,mmm for SRT."""
    if seconds < 0:
        seconds = 0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:                              # rounding overflow
        ms = 0
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt_file(blocks: List[SrtBlock], path: Path) -> None:
    """Write an SRT file consumable by ffmpeg's `subtitles=` filter."""
    lines: List[str] = []
    for i, block in enumerate(blocks, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_timecode(block.start)} --> {_srt_timecode(block.end)}")
        lines.append(block.text.replace("\r\n", "\n"))
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------- Filter fragment builders ----------

def _drone_filter(input_idx: int, label: str, clip: DroneClip,
                  out_w: int, out_h: int, fps: int) -> str:
    """
    Build the filter for a single drone clip. Implements boomerang (reverse +
    concat) plus scaling and fps normalization.
    """
    base = f"[{input_idx}:v]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease," \
           f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={fps}"

    if clip.boomerang:
        return (
            f"{base},split[{label}_fwd][{label}_src];"
            f"[{label}_src]reverse,setpts=PTS-STARTPTS[{label}_rev];"
            f"[{label}_fwd][{label}_rev]concat=n=2:v=1[{label}]"
        )
    return f"{base}[{label}]"


def _visual_filter(input_idx: int, label: str, clip: VisualClip,
                   out_w: int, out_h: int, fps: int) -> str:
    """
    Build the filter for a single visual clip (image or video).

    Images: loop into a fixed-duration clip with zoompan / flip / static.
    Videos: scale + fps normalize + optional flip.
    """
    duration = clip.duration
    frames = max(1, int(round(duration * fps)))

    if clip.kind == "image":
        head = (
            f"[{input_idx}:v]loop=loop=-1:size=1:start=0,"
            f"setpts=N/{fps}/TB,trim=duration={duration:.3f},"
            f"scale={out_w * 2}:{out_h * 2}:force_original_aspect_ratio=increase"
        )
    else:
        head = f"[{input_idx}:v]scale={out_w * 2}:{out_h * 2}:force_original_aspect_ratio=increase,fps={fps}"

    if clip.effect == "zoompan_in":
        body = (
            f"zoompan=z='min(zoom+0.0009,1.5)':d={frames}:s={out_w}x{out_h}:fps={fps}"
        )
    elif clip.effect == "zoompan_out":
        body = (
            f"zoompan=z='if(lte(zoom,1.0),1.5,max(zoom-0.0009,1.0))':d={frames}:s={out_w}x{out_h}:fps={fps}"
        )
    elif clip.effect == "flip_h":
        body = f"hflip,scale={out_w}:{out_h}:force_original_aspect_ratio=decrease," \
               f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black"
    elif clip.effect == "flip_v":
        body = f"vflip,scale={out_w}:{out_h}:force_original_aspect_ratio=decrease," \
               f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black"
    else:                                       # static
        body = (
            f"scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
            f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black"
        )

    tail = f"setsar=1,fps={fps},trim=duration={duration:.3f},setpts=PTS-STARTPTS"

    return f"{head},{body},{tail}[{label}]"


def _concat_filter(labels: List[str], out_label: str) -> str:
    inputs = "".join(f"[{lbl}]" for lbl in labels)
    return f"{inputs}concat=n={len(labels)}:v=1:a=0[{out_label}]"


def _overlay_filter(base_label: str, overlay_input_idx: int,
                    overlay: OverlayRec, out_label: str) -> str:
    """Composite the REC indicator on top of the main video using chroma key."""
    # The green-screen REC is keyed out and overlaid at (x, y).
    return (
        f"[{overlay_input_idx}:v]"
        f"chromakey=0x00FF00:0.2:0.1,"
        f"format=yuva420p[rec_keyed];"
        f"[{base_label}][rec_keyed]overlay={overlay.x}:{overlay.y}:shortest=0[{out_label}]"
    )


def _subtitle_filter(base_label: str, srt_path: Path, font_path: str,
                     out_label: str) -> str:
    """Burn the SRT into the video using the `subtitles=` filter (libass)."""
    # ffmpeg parses filter args delimited by commas; the SRT path must be escaped.
    # subtitles filter expects a path. We use the absolute path with backslashes escaped.
    safe = str(srt_path).replace("\\", "/").replace(":", r"\:")
    font_dir = str(Path(font_path).parent).replace("\\", "/").replace(":", r"\:")
    font_name = Path(font_path).stem
    style = (
        f"Fontname={font_name},FontSize=42,"
        "PrimaryColour=&H00FFFFFF&,OutlineColour=&H00000000&,"
        "BackColour=&H80000000&,Bold=1,BorderStyle=1,Outline=2,Shadow=1,"
        "Alignment=2,MarginV=80"
    )
    return f"[{base_label}]subtitles=filename='{safe}':fontsdir='{font_dir}':force_style='{style}'[{out_label}]"


# ---------- Top-level builder ----------

@dataclass
class BuiltCommand:
    argv: List[str]
    filter_complex: str
    final_video_label: str
    inputs: List[str]
    audio_input_idx: int
    output_path: Path


def build_ffmpeg_command(
    cfg: RenderConfig,
    workdir: Path,
    full_audio_path: Path,
    srt_path: Path,
    max_threads: int = 0,
) -> BuiltCommand:
    """
    Assemble the full ffmpeg invocation for a single-pass render.

    Inputs are expected to already be present locally under `workdir`:
      - full_audio_path  (concat of all audio files via demuxer, -c copy)
      - workdir / drones / <DroneClip.source>
      - workdir / visuais / <VisualClip.source>
      - workdir / overlays / <OverlayRec.source>   (if overlay enabled)
      - srt_path                                   (already written if SRT enabled)
    """
    out_w = cfg.output.width
    out_h = cfg.output.height
    fps = cfg.output.fps

    # ----- assemble -i inputs in a stable order -----
    inputs: List[str] = []
    inputs.append(str(full_audio_path))         # idx 0 — audio
    audio_idx = 0

    drone_inputs: List[Tuple[int, DroneClip]] = []
    for clip in cfg.drones:
        inputs.append(str(workdir / "drones" / clip.source))
        drone_inputs.append((len(inputs) - 1, clip))

    visual_inputs: List[Tuple[int, VisualClip]] = []
    for clip in cfg.visuals:
        inputs.append(str(workdir / "visuais" / clip.source))
        visual_inputs.append((len(inputs) - 1, clip))

    overlay_idx: int | None = None
    if cfg.overlayRec is not None:
        inputs.append(str(workdir / "overlays" / cfg.overlayRec.source))
        overlay_idx = len(inputs) - 1

    # ----- build the filter_complex graph -----
    fragments: List[str] = []
    clip_labels: List[str] = []

    # Drones first in the timeline (typical intro), then visuals.
    for i, (idx, clip) in enumerate(drone_inputs):
        label = f"d{i}"
        fragments.append(_drone_filter(idx, label, clip, out_w, out_h, fps))
        clip_labels.append(label)

    for i, (idx, clip) in enumerate(visual_inputs):
        label = f"v{i}"
        fragments.append(_visual_filter(idx, label, clip, out_w, out_h, fps))
        clip_labels.append(label)

    # Concat all clips in timeline order.
    if len(clip_labels) > 1:
        fragments.append(_concat_filter(clip_labels, "vmain"))
        current = "vmain"
    else:
        # Rewire the single clip's label to "vmain" for downstream consistency.
        # We do this by appending a null filter (no-op) so naming is uniform.
        fragments.append(f"[{clip_labels[0]}]null[vmain]")
        current = "vmain"

    # REC overlay (optional)
    if overlay_idx is not None and cfg.overlayRec is not None:
        fragments.append(_overlay_filter(current, overlay_idx, cfg.overlayRec, "vrec"))
        current = "vrec"

    # Subtitles (optional)
    if cfg.srtBlocks:
        fragments.append(_subtitle_filter(current, srt_path, cfg.fontPath, "vfinal"))
        current = "vfinal"

    # Ensure the final label is always "vfinal" for clean -map.
    if current != "vfinal":
        fragments.append(f"[{current}]null[vfinal]")

    filter_complex = ";".join(fragments)

    # ----- assemble argv -----
    output_path = workdir / cfg.output.filename
    argv: List[str] = ["ffmpeg", "-hide_banner", "-y"]

    # Add -i for each input. Audio is a regular input; images may need -loop 1
    # but loop=-1 inside the filter graph already handles that, so plain -i works.
    for path in inputs:
        argv += ["-i", path]

    argv += [
        "-filter_complex", filter_complex,
        "-map", "[vfinal]",
        "-map", f"{audio_idx}:a:0",
        # Video encode — the single consolidated libx264 pass.
        "-c:v", "libx264",
        "-preset", "faster",
        "-crf", str(cfg.antiFingerprint.crf_final),
        "-pix_fmt", cfg.output.pix_fmt,
        "-threads", str(max_threads),
        "-x264-params", "sliced-threads=1:lookahead-threads=2",
        # Audio encode — preserves the random bitrate picked by n8n.
        "-c:a", "aac",
        "-b:a", cfg.antiFingerprint.audio_bitrate,
        # Streaming-friendly mp4.
        "-movflags", "+faststart",
        # Cap render at the audio length (the audio drives total duration).
        "-shortest",
        str(output_path),
    ]

    return BuiltCommand(
        argv=argv,
        filter_complex=filter_complex,
        final_video_label="vfinal",
        inputs=inputs,
        audio_input_idx=audio_idx,
        output_path=output_path,
    )


def render_command_for_shell(cmd: BuiltCommand) -> str:
    """Return a copy-pasteable shell string (debug helper)."""
    return " ".join(shlex.quote(a) for a in cmd.argv)
