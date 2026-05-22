"""
Unit tests for ffmpeg_builder. These do not actually invoke ffmpeg — they only
verify that the assembled command has the expected shape and the right flags.

The smoke test that *does* run ffmpeg lives in tests/smoke/ and is gated to
CI/local-with-ffmpeg only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config_schema import RenderConfig
from app.ffmpeg_builder import build_ffmpeg_command, write_srt_file, _srt_timecode


FIXTURE = Path(__file__).parent / "fixtures" / "config_sample.json"


def load_cfg() -> RenderConfig:
    return RenderConfig.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))


def test_command_has_one_pass_only(tmp_path: Path) -> None:
    cfg = load_cfg()
    built = build_ffmpeg_command(
        cfg=cfg,
        workdir=tmp_path,
        full_audio_path=tmp_path / "full_audio.mp3",
        srt_path=tmp_path / "subtitles.srt",
    )
    # Exactly one ffmpeg invocation produces ONE output. No intermediate -y tmp files.
    assert built.argv[0] == "ffmpeg"
    assert built.argv.count("-filter_complex") == 1
    # The output is the final positional argument (everything before is flagged).
    assert built.argv[-1].endswith("final.mp4")
    assert built.argv[-1] == str(built.output_path)


def test_uses_required_codec_flags(tmp_path: Path) -> None:
    cfg = load_cfg()
    built = build_ffmpeg_command(
        cfg=cfg,
        workdir=tmp_path,
        full_audio_path=tmp_path / "full_audio.mp3",
        srt_path=tmp_path / "subtitles.srt",
    )
    argv = built.argv
    assert "libx264" in argv
    assert "faster" in argv                     # preset=faster per spec
    assert str(cfg.antiFingerprint.crf_final) in argv
    assert cfg.antiFingerprint.audio_bitrate in argv
    assert "aac" in argv
    assert "+faststart" in argv
    assert "-threads" in argv
    assert "-x264-params" in argv


def test_filter_graph_concats_all_clips(tmp_path: Path) -> None:
    cfg = load_cfg()
    built = build_ffmpeg_command(
        cfg=cfg,
        workdir=tmp_path,
        full_audio_path=tmp_path / "full_audio.mp3",
        srt_path=tmp_path / "subtitles.srt",
    )
    fc = built.filter_complex
    # 1 drone + 3 visuals -> 4 clips concatenated
    assert "concat=n=4" in fc
    # The REC overlay must be wired in
    assert "overlay=" in fc
    # Subtitles must be wired in
    assert "subtitles=" in fc
    # Final label is vfinal
    assert "[vfinal]" in fc


def test_input_order_audio_then_drones_then_visuals_then_overlay(tmp_path: Path) -> None:
    cfg = load_cfg()
    built = build_ffmpeg_command(
        cfg=cfg,
        workdir=tmp_path,
        full_audio_path=tmp_path / "full_audio.mp3",
        srt_path=tmp_path / "subtitles.srt",
    )
    inputs = built.inputs
    assert inputs[0].endswith("full_audio.mp3")
    assert "drones" in inputs[1]
    assert "visuais" in inputs[2]
    assert "visuais" in inputs[3]
    assert "visuais" in inputs[4]
    assert "overlays" in inputs[5]


def test_srt_timecode_formatting() -> None:
    assert _srt_timecode(0) == "00:00:00,000"
    assert _srt_timecode(1.5) == "00:00:01,500"
    assert _srt_timecode(3661.123) == "01:01:01,123"


def test_write_srt_roundtrip(tmp_path: Path) -> None:
    cfg = load_cfg()
    srt = tmp_path / "out.srt"
    write_srt_file(cfg.srtBlocks, srt)
    text = srt.read_text(encoding="utf-8")
    assert "1\n00:00:00,000 --> 00:00:04,500\nLinha de teste 1" in text
    assert "2\n00:00:04,500 --> 00:00:09,000" in text


def test_no_overlay_no_subtitles_still_yields_vfinal(tmp_path: Path) -> None:
    cfg = load_cfg()
    cfg.overlayRec = None
    cfg.srtBlocks = []
    built = build_ffmpeg_command(
        cfg=cfg,
        workdir=tmp_path,
        full_audio_path=tmp_path / "full_audio.mp3",
        srt_path=tmp_path / "subtitles.srt",
    )
    assert "[vfinal]" in built.filter_complex
    assert "overlay=" not in built.filter_complex
    assert "subtitles=" not in built.filter_complex
