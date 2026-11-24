# Distributed Video Lab Prototype

A runnable prototype: upload a video, choose 1080p/720p/480p/360p, split into ~6-second source chunks, create one task per chunk × quality, distribute those tasks to independent worker laptops, transcode with FFmpeg, upload HLS segments, and generate `.m3u8` playlists. Workers heartbeat; stale processing tasks are requeued.

## Run coordinator
Install FFmpeg and Python 3.11+. Then:

```bash
cd app
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`.

## Run workers
In separate terminals:

```bash
cd worker
pip install -r requirements.txt
python worker.py --coordinator http://127.0.0.1:8000 --name worker-1
python worker.py --coordinator http://127.0.0.1:8000 --name worker-2
python worker.py --coordinator http://127.0.0.1:8000 --name worker-3
```

For a second laptop, replace `127.0.0.1` with the coordinator laptop's LAN IP.
The worker laptop runs its own worker process and points it at the **coordinator's** LAN IP; do not enter the friend's IP into the coordinator. The dashboard's **Connect a worker laptop** panel generates the command to run remotely. Both devices must be on a trusted LAN, and TCP port 8000 must be allowed through the coordinator's firewall.

## Core architecture
Browser -> FastAPI Coordinator -> source storage -> FFmpeg chunker -> task queue/table -> Worker Nodes -> FFmpeg -> output storage -> HLS playlists.

The prototype intentionally uses SQLite + HTTP polling for easy demonstration. The worker contract is separated so production can replace these with PostgreSQL + Redis/RabbitMQ and S3/MinIO without changing the main distributed workflow.

## Intelligent architecture

The coordinator now performs chunk-level FFmpeg analysis, computes motion and spatial complexity, plans a profile-specific target bitrate, and records task scheduling decisions. The active scheduler mode is:

- `intelligent`: capacity + CPU load + queue occupancy + network cost + task priority
- `baseline`: simple first-available-task scheduling

Set the baseline mode when starting the coordinator:

```bash
set SCHEDULER_MODE=baseline
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

It can also be switched live with `POST /api/scheduler/mode`:

```json
{"mode":"intelligent"}
```

Useful APIs include:

- `GET /api/jobs/{job_id}/analysis`
- `GET /api/jobs/{job_id}/metrics`
- `GET /api/scheduler/status`
- `GET /api/scheduler/decisions`
- `GET /api/jobs/{job_id}/master.m3u8`

The complexity formula currently used is `0.6M + 0.4S`; saliency is not claimed or measured.

## Code modules

The browser UI is split into focused ES modules under `app/static/js/`:

- `upload.js` — upload form and rendition selection
- `jobs.js` — job progress polling
- `player.js` — output cards and HLS playback
- `workers.js` — worker telemetry
- `worker_onboarding.js` — remote worker command generation
- `scheduler.js` — scheduling mode, decisions, and job timings
- `state.js` and `utils.js` — shared state and HTTP/HTML helpers

Backend analysis and scheduling logic live in `app/content_analysis.py` and `app/scheduler.py`.
