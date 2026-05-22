# rede-z-renderer

FFmpeg render service for the **Rede Z** automation pipeline. Runs as a
self-contained container (3rd Easypanel slot, sibling of `n8n` and
`edge-tts`). The n8n flow `Rede Z - Editor.json` posts a `RenderConfig`
JSON to `POST /render`; this service downloads assets via rclone, runs a
single-pass FFmpeg, uploads the result to Google Drive, and calls back
into `Rede Z - Webhook.json` (`/webhook/render-done`).

## Why this exists

The legacy bash pipeline did 4-5 cascading `libx264` re-encodes (drone →
effects → intro → overlay → concat → final) all running synchronously
inside an n8n SSH node. A 2h video took 24-48h to render and n8n stayed
blocked the entire time.

This service collapses everything into **one FFmpeg invocation** with a
consolidated `-filter_complex` graph, runs it in the background, and
notifies n8n via webhook when done. Expected speedup: **60-75%** on CPU-only
hardware. See [the project plan](../Área%20de%20Trabalho/Youtube%20Revenge/Rede%20Z/_renderer_repo_link.md)
for the full rationale.

## Endpoints

```
GET  /health                  → {"status": "ok", ...}
POST /render                  → 202 Accepted, {"jobId": "...", "pid": N}
GET  /status/{job_id}         → JobStatus JSON (queued|downloading|rendering|uploading|ok|error)
GET  /logs/{job_id}?tail=200  → last N lines of ffmpeg.log
```

The full POST body schema is in `app/config_schema.py`. A working sample
lives at `tests/fixtures/config_sample.json`.

## Local dev

```bash
# Build & run
docker build -t rede-z-renderer .
docker run --rm -p 8080:8080 \
  -e CALLBACK_TOKEN=dev \
  -v $PWD/local-rclone.conf:/root/.config/rclone/rclone.conf:ro \
  rede-z-renderer

# Smoke test
curl http://localhost:8080/health
curl -X POST -H 'Content-Type: application/json' \
  -d @tests/fixtures/config_sample.json \
  http://localhost:8080/render
```

## Deployment (Easypanel slot 3)

1. Push to `main` on this repo's GitHub.
2. Easypanel slot 3 is configured with GitHub source + auto-deploy → rebuilds.
3. Internal hostname `rede-z-renderer:8080` is what the n8n container calls.

Required secrets (set in Easypanel UI):
- `CALLBACK_TOKEN` — shared with `Rede Z - Webhook.json` (header `X-Render-Token`)
- The rclone config is mounted from a secret volume to
  `/root/.config/rclone/rclone.conf`

## Debugging a stuck job

```bash
# From the host (Easypanel terminal):
docker exec rede-z-renderer ls /tmp/rede-z
docker exec rede-z-renderer cat /tmp/rede-z/<jobId>/status.json
docker exec rede-z-renderer tail -f /tmp/rede-z/<jobId>/ffmpeg.log

# Or via the HTTP API:
curl http://rede-z-renderer:8080/status/<jobId>
curl http://rede-z-renderer:8080/logs/<jobId>?tail=500
```

The exact ffmpeg command used for any job is saved at
`/tmp/rede-z/<jobId>/ffmpeg_command.sh` for reproduction.

## Tests

```bash
pip install -r requirements.txt pytest
pytest tests/
```

## Project layout

```
app/
  main.py           FastAPI app: /health /render /status /logs
  config_schema.py  Pydantic models for the request body and JobStatus
  ffmpeg_builder.py Consolidated single-pass filter_complex generator
  rclone_client.py  Parallel download + upload via rclone
  pipeline.py       End-to-end orchestration
  callback.py       POST status back to n8n with retries
tests/
  test_ffmpeg_builder.py
  test_schema.py
  fixtures/config_sample.json
```
