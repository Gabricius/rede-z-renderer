"""
POST the completion (or failure) status back to the n8n webhook.

The n8n side validates `X-Render-Token` against a shared secret before letting
the cascade continue. We send token from env (CALLBACK_TOKEN) unless the
config explicitly overrides it.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from app.config_schema import JobStatus

log = logging.getLogger(__name__)


async def notify(callback_url: str, status: JobStatus,
                 token: Optional[str] = None,
                 timeout_seconds: float = 30.0) -> None:
    """Fire-and-log the callback. Retries 3x with backoff on transient errors."""
    headers = {"Content-Type": "application/json"}
    effective_token = token or os.environ.get("CALLBACK_TOKEN")
    if effective_token:
        headers["X-Render-Token"] = effective_token

    payload = status.model_dump(mode="json")

    delays = [0.0, 2.0, 8.0]
    last_exc: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        for i, delay in enumerate(delays):
            if delay:
                import asyncio
                await asyncio.sleep(delay)
            try:
                resp = await client.post(callback_url, json=payload, headers=headers)
                if 200 <= resp.status_code < 300:
                    log.info("callback ok: %s -> %s", callback_url, resp.status_code)
                    return
                log.warning("callback non-2xx (attempt %d): status=%d body=%s",
                            i + 1, resp.status_code, resp.text[:300])
                last_exc = RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            except httpx.HTTPError as e:
                log.warning("callback error (attempt %d): %s", i + 1, e)
                last_exc = e

    log.error("callback exhausted retries: %s", last_exc)
