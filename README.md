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

## Core architecture
Browser -> FastAPI Coordinator -> source storage -> FFmpeg chunker -> task queue/table -> Worker Nodes -> FFmpeg -> output storage -> HLS playlists.

The prototype intentionally uses SQLite + HTTP polling for easy demonstration. The worker contract is separated so production can replace these with PostgreSQL + Redis/RabbitMQ and S3/MinIO without changing the main distributed workflow.
