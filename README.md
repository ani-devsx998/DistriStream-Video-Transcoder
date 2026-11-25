# DISTRISTREAM

A runnable prototype: upload a video, choose 1080p/720p/480p/360p, split into ~6-second source chunks, create one task per chunk × quality, distribute those tasks to independent worker laptops, transcode with FFmpeg, upload HLS segments, and generate `.m3u8` playlists. Workers heartbeat; stale processing tasks are requeued.

## Run coordinator
Install FFmpeg and Python 3.11+. Then:

```bash
cd <project-root>
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r app/requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000`.

## Run workers
In separate terminals:

```bash
cd <project-root>\worker
python -m pip install -r requirements.txt
python worker.py --coordinator http://127.0.0.1:8000 --name worker-1
python worker.py --coordinator http://127.0.0.1:8000 --name worker-2
python worker.py --coordinator http://127.0.0.1:8000 --name worker-3
```

For a second laptop, replace `127.0.0.1` with the coordinator laptop's LAN IP.
Each worker laptop runs its own worker process and points it at the **coordinator's** LAN IP. For example, if the coordinator is `192.168.0.107`, run this on the worker laptop:

```powershell
cd <project-folder>\worker
python -m pip install -r requirements.txt
python worker.py --coordinator http://192.168.0.107:8000 --name Worker-1
```

Use the coordinator's current LAN IPv4 address if it differs; do not use `127.0.0.1` from another laptop. To run a worker on the coordinator laptop itself, use `http://127.0.0.1:8000` in a separate terminal. The **New Job** page's worker setup section generates remote worker commands. Both devices must be on a reachable network, and TCP port 8000 must be allowed through the coordinator's firewall.

To check connectivity from a worker laptop, run `Test-NetConnection 192.168.0.107 -Port 8000` in PowerShell. `TcpTestSucceeded` should be `True`. The coordinator must be running with `--host 0.0.0.0` so other machines can reach it.

The left navigation includes **Task Distribution**, which displays live completed, queued, assigned, and failed task totals in a doughnut chart. The page shows the distribution chart without a task routing table.

## Deploy to Vercel

Set Vercel's **Root Directory** to the repository root. The root `main.py` exports the FastAPI application, and the root `requirements.txt` installs its dependencies. Vercel should route the deployed site to the FastAPI app at `/`.

The current prototype stores job data and uploaded videos locally and invokes FFmpeg. Vercel Functions use temporary storage and have request size and execution limits, so a successful deployment does not make the full distributed transcoding workflow production-ready. Use durable external storage and a persistent database/worker service for real video jobs.

## Core architecture
Browser -> FastAPI Coordinator -> source storage -> FFmpeg chunker -> task queue/table -> Worker Nodes -> FFmpeg -> output storage -> HLS playlists.

The prototype intentionally uses SQLite + HTTP polling for easy demonstration. The worker contract is separated so production can replace these with PostgreSQL + Redis/RabbitMQ and S3/MinIO without changing the main distributed workflow.

## Intelligent architecture

The coordinator now performs chunk-level FFmpeg analysis, computes motion and spatial complexity, plans a profile-specific target bitrate, and records task scheduling decisions. The active scheduler mode is:

- `intelligent`: capacity + CPU load + queue occupancy + network cost + task priority
- `baseline`: simple first-available-task scheduling

Set the baseline mode when starting the coordinator from the project root:

```bash
set SCHEDULER_MODE=baseline
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
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

- `app.js` - page routing, dashboard, and task distribution doughnut chart
- `upload.js` — upload form and rendition selection
- `jobs.js` — job progress polling
- `player.js` — output cards and HLS playback
- `workers.js` — worker telemetry
- `worker_onboarding.js` — remote worker command generation
- `scheduler.js` — scheduling mode, decisions, and job timings
- `state.js` and `utils.js` — shared state and HTTP/HTML helpers

Backend analysis and scheduling logic live in `app/content_analysis.py` and `app/scheduler.py`.
