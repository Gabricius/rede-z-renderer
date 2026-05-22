"""Smoke tests that the Pydantic schema parses the fixture correctly."""

from __future__ import annotations

import json
from pathlib import Path

from app.config_schema import RenderConfig


FIXTURE = Path(__file__).parent / "fixtures" / "config_sample.json"


def test_fixture_parses() -> None:
    cfg = RenderConfig.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert cfg.jobId == "test-job-001"
    assert len(cfg.visuals) == 3
    assert cfg.antiFingerprint.crf_final == 22


def test_crf_bounds() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw["antiFingerprint"]["crf_final"] = 17        # below the gentle lower bound
    try:
        RenderConfig.model_validate(raw)
    except Exception:
        return
    raise AssertionError("expected validation to reject crf_final=17")
