import asyncio
import json
import sqlite3
import subprocess
import time
import uuid

from pathlib import Path
import os

from app.content_analysis import analyze_chunk, plan_bitrate
import app.scheduler as scheduler_module
from app.scheduler import (
    QUEUE_CAPACITY,
    SCHEDULER_MODE,
    choose_worker,
    priority_for,
)

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)

from fastapi.responses import (
    FileResponse,
    HTMLResponse,
)

from fastapi.staticfiles import StaticFiles


# ============================================================
# PATHS
# ============================================================

BASE = Path(__file__).resolve().parent.parent

ST = BASE / "storage"

UP = ST / "uploads"
CH = ST / "chunks"
OUT = ST / "outputs"

DB = ST / "prototype.db"


for directory in (UP, CH, OUT):
    directory.mkdir(
        parents=True,
        exist_ok=True
    )


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="Distributed Video Lab"
)

app.mount(
    "/static",
    StaticFiles(
        directory=BASE / "app" / "static"
    ),
    name="static",
)


# ============================================================
# DATABASE
# ============================================================

def db():
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    return connection


def init():

    connection = db()

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY,
            filename TEXT,
            source TEXT,
            duration REAL,
            status TEXT,
            profiles TEXT,
            total INTEGER,
            done INTEGER DEFAULT 0,
            created REAL
        );

        CREATE TABLE IF NOT EXISTS tasks(
            id TEXT PRIMARY KEY,
            job_id TEXT,
            chunk INTEGER,
            chunk_path TEXT,
            profile TEXT,
            status TEXT DEFAULT 'queued',
            worker TEXT,
            attempts INTEGER DEFAULT 0,
            started REAL,
            output TEXT,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS workers(
            id TEXT PRIMARY KEY,
            name TEXT,
            host TEXT,
            status TEXT,
            last_seen REAL,
            active TEXT,
            cpu INTEGER,
            gpu TEXT
        );

        CREATE TABLE IF NOT EXISTS scheduler_decisions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            job_id TEXT,
            chunk INTEGER,
            profile TEXT,
            complexity REAL,
            priority REAL,
            mode TEXT,
            selected_worker TEXT,
            score REAL,
            candidates TEXT,
            created REAL
        );
        """
    )

    migrations = {
        "jobs": [
            ("upload_time", "REAL"),
            ("chunking_time", "REAL"),
            ("analysis_time", "REAL"),
            ("started_at", "REAL"),
            ("completed_at", "REAL"),
            ("scheduler_mode", "TEXT"),
            ("manual_workers", "TEXT"),
        ],
        "tasks": [
            ("complexity", "REAL"),
            ("motion", "REAL"),
            ("spatial", "REAL"),
            ("priority", "REAL"),
            ("target_bitrate", "INTEGER"),
            ("queue_time", "REAL"),
            ("download_time", "REAL"),
            ("processing_time", "REAL"),
            ("upload_time", "REAL"),
            ("completed_at", "REAL"),
        ],
        "workers": [
            ("ram", "REAL"),
            ("cpu_usage", "REAL"),
            ("gpu_usage", "REAL"),
            ("queue_depth", "INTEGER"),
            ("max_queue", "INTEGER"),
            ("network_cost", "REAL"),
            ("tasks_completed", "INTEGER DEFAULT 0"),
            ("tasks_failed", "INTEGER DEFAULT 0"),
            ("tasks_recovered", "INTEGER DEFAULT 0"),
            ("disconnect_requested", "INTEGER DEFAULT 0"),
        ],
    }

    for table, columns in migrations.items():
        existing = {
            row["name"]
            for row in connection.execute(
                f"PRAGMA table_info({table})"
            )
        }
        for name, definition in columns:
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE {table} "
                    f"ADD COLUMN {name} {definition}"
                )

    connection.commit()
    connection.close()


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup():

    init()

    asyncio.create_task(
        reaper()
    )


# ============================================================
# FRONTEND
# ============================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
def index():

    return FileResponse(
        BASE / "app" / "static" / "index.html"
    )


# ============================================================
# WORKERS
# ============================================================

@app.get("/api/workers")
def workers():

    connection = db()

    rows = connection.execute(
        "SELECT * FROM workers"
    ).fetchall()

    connection.close()

    return [
        dict(row)
        for row in rows
    ]


@app.post("/api/workers/register")
async def register(
    request: Request
):

    data = await request.json()

    worker_id = (
        data.get("id")
        or data.get("worker_id")
        or data.get("wid")
        or data.get("name")
    )

    if not worker_id:

        raise HTTPException(
            status_code=400,
            detail="Worker ID is required"
        )

    connection = db()

    connection.execute(
        """
        INSERT INTO workers
        (id, name, host, status, last_seen, active, cpu, gpu,
         ram, cpu_usage, gpu_usage, queue_depth, max_queue,
         network_cost)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,
            host=excluded.host,
            status='idle',
            disconnect_requested=0,
            last_seen=excluded.last_seen,
            cpu=excluded.cpu,
            gpu=excluded.gpu,
            ram=COALESCE(excluded.ram, workers.ram),
            max_queue=COALESCE(excluded.max_queue, workers.max_queue)
        """,
        (
            worker_id,
            data.get(
                "name",
                worker_id
            ),
            data.get(
                "host",
                ""
            ),
            "idle",
            time.time(),
            None,
            data.get(
                "cpu",
                1
            ),
            data.get("gpu", "unknown"),
            data.get("ram"),
            data.get("cpu_usage", 0),
            data.get("gpu_usage", 0),
            data.get("queue_depth", 0),
            data.get("max_queue", QUEUE_CAPACITY),
            data.get("network_cost", 0),
        ),
    )

    connection.commit()
    connection.close()

    return {
        "ok": True,
        "worker_id": worker_id
    }


@app.post(
    "/api/workers/{wid}/heartbeat"
)
async def heartbeat(
    wid: str,
    request: Request
):
    data = await request.json()
    connection = db()
    worker = connection.execute(
        "SELECT disconnect_requested FROM workers WHERE id=?",
        (wid,),
    ).fetchone()

    if not worker:
        connection.close()
        return {"ok": False, "disconnect": True}

    if worker["disconnect_requested"]:
        connection.execute(
            "UPDATE workers SET status='offline', active=NULL, last_seen=? WHERE id=?",
            (time.time(), wid),
        )
        connection.commit()
        connection.close()
        return {"ok": True, "disconnect": True}

    connection.execute(
        """
        UPDATE workers
        SET status=?, last_seen=?, active=?, cpu_usage=?, gpu_usage=?, ram=?,
            queue_depth=?, network_cost=?, gpu=COALESCE(?, gpu)
        WHERE id=?
        """,
        (
            data.get("status", "idle"), time.time(), data.get("active"),
            data.get("cpu_usage", 0), data.get("gpu_usage", 0), data.get("ram"),
            data.get("queue_depth", 0), data.get("network_cost", 0),
            data.get("gpu"), wid,
        ),
    )
    connection.commit()
    connection.close()
    return {"ok": True, "disconnect": False}



@app.get("/api/scheduler/status")
def scheduler_status():
    connection = db()
    mode = os.getenv("SCHEDULER_MODE", "intelligent")
    queued = connection.execute(
        "SELECT COUNT(*) FROM tasks WHERE status='queued'"
    ).fetchone()[0]
    assigned = connection.execute(
        "SELECT COUNT(*) FROM tasks WHERE status!='queued' AND status!='completed'"
    ).fetchone()[0]
    decisions = connection.execute(
        "SELECT COUNT(*) FROM scheduler_decisions"
    ).fetchone()[0]
    connection.close()
    return {
        "mode": mode,
        "queue_capacity": QUEUE_CAPACITY,
        "queued_tasks": queued,
        "active_tasks": assigned,
        "decisions": decisions,
        "formula": "0.25P + 0.30K - 0.20L - 0.15Q - 0.10N",
    }


@app.post("/api/scheduler/mode")
async def scheduler_mode(request: Request):
    data = await request.json()
    mode = str(data.get("mode", "intelligent")).lower()
    if mode not in ("baseline", "intelligent"):
        raise HTTPException(status_code=400, detail="Mode must be baseline or intelligent")

    os.environ["SCHEDULER_MODE"] = mode
    scheduler_module.SCHEDULER_MODE = mode
    return {"mode": mode}


@app.get("/api/scheduler/decisions")
def scheduler_decisions(limit: int = 5):
    connection = db()
    rows = connection.execute(
        """
        SELECT * FROM scheduler_decisions
        ORDER BY created DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    connection.close()
    result = []
    for row in rows:
        item = dict(row)
        item["candidates"] = json.loads(item["candidates"] or "[]")
        result.append(item)
    return result


# ============================================================
# VIDEO JOB
# ============================================================

@app.get("/api/jobs/active")
def active_job():
    connection = db()
    row = connection.execute(
        "SELECT id, status FROM jobs WHERE status IN ('queued', 'processing') ORDER BY created DESC LIMIT 1"
    ).fetchone()
    connection.close()
    return dict(row) if row else None


@app.get("/api/jobs/{jid}")

def job(jid: str):

    connection = db()

    job_row = connection.execute(
        "SELECT * FROM jobs WHERE id=?",
        (jid,),
    ).fetchone()

    if not job_row:

        connection.close()

        raise HTTPException(
            status_code=404,
            detail="Job not found"
        )

    tasks = connection.execute(
        """
        SELECT
            id,
            chunk,
            profile,
            status,
            worker,
            attempts,
            error,
            complexity,
            motion,
            spatial,
            priority,
            target_bitrate,
            queue_time,
            download_time,
            processing_time,
            upload_time,
            started,
            completed_at
        FROM tasks
        WHERE job_id=?
        ORDER BY chunk, profile
        """,
        (jid,),
    ).fetchall()

    connection.close()

    result = dict(job_row)

    result["profiles"] = json.loads(
        result["profiles"]
    )

    result["tasks"] = [
        dict(task)
        for task in tasks
    ]

    return result


def purge_stopped_job_tasks(connection, jid, now):
    connection.execute(
        "UPDATE workers SET status='offline', disconnect_requested=1, active=NULL, last_seen=? "
        "WHERE id IN (SELECT worker FROM tasks WHERE job_id=? AND worker IS NOT NULL) "
        "OR (last_seen>=? AND status!='offline')",
        (now, jid, now - 20),
    )
    connection.execute(
        "DELETE FROM scheduler_decisions WHERE job_id=?",
        (jid,),
    )
    connection.execute(
        "DELETE FROM tasks WHERE job_id=?",
        (jid,),
    )


@app.post("/api/jobs/{jid}/cancel")
def cancel_job(jid: str):
    connection = db()
    job_row = connection.execute(
        "SELECT status FROM jobs WHERE id=?",
        (jid,),
    ).fetchone()
    if not job_row:
        connection.close()
        raise HTTPException(status_code=404, detail="Job not found")
    if job_row["status"] == "completed":
        connection.close()
        raise HTTPException(status_code=409, detail="Completed jobs cannot be cancelled")
    if job_row["status"] == "cancelled":
        connection.close()
        return {"ok": True, "status": "cancelled"}
    now = time.time()
    purge_stopped_job_tasks(connection, jid, now)
    connection.execute(
        "UPDATE jobs SET status='cancelled', completed_at=?, done=0, total=0 WHERE id=?",
        (now, jid),
    )
    connection.commit()
    connection.close()
    return {"ok": True, "status": "cancelled"}


@app.get("/api/jobs/{jid}/analysis")
def job_analysis(jid: str):
    connection = db()
    rows = connection.execute(
        """
        SELECT chunk, AVG(motion) AS motion, AVG(spatial) AS spatial,
               AVG(complexity) AS complexity, AVG(priority) AS priority,
               AVG(target_bitrate) AS target_bitrate
        FROM tasks
        WHERE job_id=?
        GROUP BY chunk
        ORDER BY chunk
        """,
        (jid,),
    ).fetchall()
    connection.close()

    chunks = []
    for row in rows:
        complexity = row["complexity"] or 0.0
        level = (
            "LOW" if complexity < 0.30
            else "MEDIUM" if complexity < 0.60
            else "HIGH" if complexity < 0.80
            else "VERY_HIGH"
        )
        chunks.append({
            "chunk_id": f"C{row['chunk'] + 1}",
            "chunk": row["chunk"],
            "motion": round(row["motion"] or 0.0, 4),
            "spatial": round(row["spatial"] or 0.0, 4),
            "complexity": round(complexity, 4),
            "level": level,
            "priority": round(row["priority"] or 0.0, 4),
            "target_bitrate": round(row["target_bitrate"] or 0),
            "formula": "0.6M + 0.4S (saliency unavailable)",
        })
    return {"job_id": jid, "chunks": chunks}


@app.get("/api/jobs/{jid}/metrics")
def job_metrics(jid: str):
    connection = db()
    job = connection.execute(
        "SELECT * FROM jobs WHERE id=?",
        (jid,),
    ).fetchone()
    if not job:
        connection.close()
        raise HTTPException(status_code=404, detail="Job not found")

    task_metrics = connection.execute(
        """
        SELECT
            COUNT(*) AS count,
            AVG(queue_time) AS avg_queue,
            AVG(download_time) AS avg_download,
            AVG(processing_time) AS avg_processing,
            AVG(upload_time) AS avg_upload
        FROM tasks
        WHERE job_id=?
        """,
        (jid,),
    ).fetchone()
    connection.close()

    total_time = None
    if job["completed_at"] and job["started_at"]:
        total_time = round(job["completed_at"] - job["started_at"], 4)

    return {
        "job_id": jid,
        "upload_time": job["upload_time"],
        "chunking_time": job["chunking_time"],
        "analysis_time": job["analysis_time"],
        "total_job_time": total_time,
        "average_queue_time": round(task_metrics["avg_queue"] or 0, 4),
        "average_download_time": round(task_metrics["avg_download"] or 0, 4),
        "average_processing_time": round(task_metrics["avg_processing"] or 0, 4),
        "average_result_upload_time": round(task_metrics["avg_upload"] or 0, 4),
        "scheduler_mode": job["scheduler_mode"],
        "completed_tasks": job["done"],
        "total_tasks": job["total"],
    }


# ============================================================
# VIDEO DURATION
# ============================================================

def duration(video_path: Path):

    try:

        result = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(video_path),
            ],
            stderr=subprocess.STDOUT,
        )

        return float(
            result.decode().strip()
        )

    except Exception:

        return 0


def video_dimensions(video_path: Path):
    """Return source video width and height from ffprobe metadata."""
    try:
        result = subprocess.check_output(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "json",
                str(video_path),
            ],
            stderr=subprocess.STDOUT,
        )
        streams = json.loads(result.decode()).get("streams", [])
        if not streams:
            return None, None
        return int(streams[0]["width"]), int(streams[0]["height"])
    except (Exception, KeyError, ValueError, TypeError):
        return None, None


# ============================================================
# UPLOAD + CHUNKING
# ============================================================

@app.post("/api/upload")
async def upload(
    video: UploadFile = File(...),
    profiles: str = Form(
        "1080p,720p,480p"
    ),
    scheduler_mode: str = Form("intelligent"),
    manual_workers: str = Form(""),
):
    upload_started = time.time()

    scheduler_mode = scheduler_mode.lower().strip()
    if scheduler_mode not in {"baseline", "intelligent", "manual"}:
        raise HTTPException(status_code=400, detail="Mode must be baseline, intelligent, or manual")
    manual_worker_names = [name.strip() for name in manual_workers.split(",") if name.strip()]
    if scheduler_mode == "manual" and not manual_worker_names:
        raise HTTPException(status_code=400, detail="Choose at least one worker for manual assignment")

    allowed = {
        "1080p",
        "720p",
        "480p",
        "360p",
    }

    selected_profiles = [
        profile
        for profile in profiles.split(",")
        if profile in allowed
    ]

    if not selected_profiles:

        raise HTTPException(
            status_code=400,
            detail="Choose a quality"
        )

    job_id = str(
        uuid.uuid4()
    )

    suffix = Path(
        video.filename or ".mp4"
    ).suffix

    source = (
        UP / f"{job_id}{suffix}"
    )

    # Save uploaded video
    with source.open("wb") as file:

        while True:

            chunk = await video.read(
                1024 * 1024
            )

            if not chunk:
                break

            file.write(chunk)

    _, source_height = video_dimensions(source)
    if not source_height:
        source.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail="Could not read the source video resolution. No job was created.",
        )

    rendition_heights = {
        "1080p": 1080,
        "720p": 720,
        "480p": 480,
        "360p": 360,
    }
    unsupported = [
        profile for profile in selected_profiles
        if rendition_heights[profile] > source_height
    ]
    if unsupported:
        source_label = max(
            (name for name, height in rendition_heights.items() if height <= source_height),
            key=lambda name: rendition_heights[name],
            default="none",
        )
        available = ", ".join(
            name for name, height in rendition_heights.items()
            if height <= source_height
        ) or "none"
        source.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=(
                f"This source supports up to {source_label}. "
                f"Higher renditions would upscale the video. Choose: {available}."
            ),
        )

    upload_time = time.time() - upload_started

    # Create chunk directory
    chunk_directory = CH / job_id

    chunk_directory.mkdir(
        parents=True,
        exist_ok=True
    )
    chunk_started = time.time()

    # Split video into approximately
    # 6-second independently processable chunks
    try:

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(source),

                "-map",
                "0:v:0",

                "-map",
                "0:a:0?",

                "-c",
                "copy",

                "-f",
                "segment",

                "-segment_time",
                "6",

                "-reset_timestamps",
                "1",

                "-segment_format",
                "mp4",

                str(
                    chunk_directory
                    / "chunk_%05d.mp4"
                ),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=(
                "FFmpeg chunking failed: "
                + str(error)[-700:]
            ),
        )

    chunks = sorted(
        chunk_directory.glob(
            "chunk_*.mp4"
        )
    )
    chunking_time = time.time() - chunk_started

    if not chunks:

        raise HTTPException(
            status_code=500,
            detail="No chunks created"
        )

    total_tasks = (
        len(chunks)
        * len(selected_profiles)
    )

    analysis_started = time.time()
    chunk_analysis = []
    for chunk_index, chunk_path in enumerate(chunks):
        try:
            metrics = analyze_chunk(chunk_path)
        except Exception as error:
            metrics = {
                "motion": 0.0,
                "spatial": 0.0,
                "complexity": 0.0,
                "level": "LOW",
                "formula": "analysis unavailable",
                "error": str(error)[-300:],
            }
        chunk_analysis.append(metrics)
    analysis_time = time.time() - analysis_started

    connection = db()

    connection.execute(
        """
        INSERT INTO jobs(
            id, filename, source, duration, status, profiles,
            total, done, created, upload_time, chunking_time,
            analysis_time, started_at, scheduler_mode, manual_workers
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            video.filename,
            str(source),
            duration(source),
            "queued",
            json.dumps(
                selected_profiles
            ),
            total_tasks,
            0,
            time.time(),
            round(upload_time, 4),
            round(chunking_time, 4),
            round(analysis_time, 4),
            time.time(),
            scheduler_mode,
            json.dumps(manual_worker_names),
        ),
    )

    # Every chunk + quality combination
    # becomes an independent task.
    for chunk_index, chunk_path in enumerate(chunks):

        for profile in selected_profiles:

            connection.execute(
                """
                INSERT INTO tasks(
                    id,
                    job_id,
                    chunk,
                    chunk_path,
                    profile,
                    complexity,
                    motion,
                    spatial,
                    priority,
                    target_bitrate,
                    status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued')
                """,
                (
                    str(
                        uuid.uuid4()
                    ),
                    job_id,
                    chunk_index,
                    str(chunk_path),
                    profile,
                    chunk_analysis[chunk_index]["complexity"],
                    chunk_analysis[chunk_index]["motion"],
                    chunk_analysis[chunk_index]["spatial"],
                    priority_for(chunk_analysis[chunk_index]["complexity"]),
                    plan_bitrate(profile, chunk_analysis[chunk_index]["complexity"]),
                ),
            )

    connection.commit()
    connection.close()

    return {
        "job_id": job_id,
        "chunks": len(chunks),
        "tasks": total_tasks,
        "profiles": selected_profiles,
    }


# ============================================================
# CLAIM TASK
# ============================================================

@app.post("/api/tasks/claim")
async def claim(
    request: Request,
    wid: str | None = None,
):

    # First try query parameter
    worker_id = wid

    # If query parameter wasn't supplied,
    # try JSON body.
    if not worker_id:

        try:

            data = await request.json()

            worker_id = (
                data.get("wid")
                or data.get("worker_id")
                or data.get("id")
                or data.get("name")
            )

        except Exception:

            worker_id = None

    if not worker_id:

        raise HTTPException(
            status_code=400,
            detail="Worker ID is required"
        )

    connection = db()

    # SQLite transaction prevents two workers
    # from claiming the same queued task.
    connection.execute(
        "BEGIN IMMEDIATE"
    )

    task = connection.execute(
        """
        SELECT tasks.*
        FROM tasks
        JOIN jobs ON jobs.id=tasks.job_id
        WHERE tasks.status='queued'
          AND jobs.status IN ('queued', 'processing')
        ORDER BY
            CASE
                WHEN tasks.priority IS NULL THEN 0
                ELSE tasks.priority
            END DESC,
            tasks.attempts ASC,
            tasks.chunk ASC
        LIMIT 1
        """
    ).fetchone()

    # No task currently available
    if not task:

        connection.commit()
        connection.close()

        return {
            "task": None
        }

    chunk_path = Path(task['chunk_path'] or '')
    if not chunk_path.is_file():
        reason = f"Input chunk is missing: {chunk_path}"
        mark_job_input_missing(connection, task["job_id"], reason)
        connection.commit()
        connection.close()
        return {"task": None}

    selected, candidates = choose_worker(connection, task)

    if not selected:
        connection.commit()
        connection.close()
        return {"task": None}

    if selected["id"] != worker_id:
        connection.commit()
        connection.close()
        return {"task": None}

    now = time.time()
    job_row = connection.execute(
        "SELECT started_at, scheduler_mode FROM jobs WHERE id=?",
        (task["job_id"],),
    ).fetchone()
    queue_time = max(
        0,
        now - (
            job_row["started_at"]
            if job_row and job_row["started_at"]
            else now
        ),
    )

    # Atomically assign the task
    connection.execute(
        """
        UPDATE tasks
        SET
            status='assigned',
            worker=?,
            attempts=attempts+1,
            started=?,
            queue_time=?
        WHERE id=?
        """,
        (
            selected["id"],
            now,
            round(queue_time, 4),
            task["id"],
        ),
    )

    # Mark worker busy
    connection.execute(
        """
        UPDATE workers
        SET
            status='busy',
            active=?,
            last_seen=?
        WHERE id=?
        """,
        (
            task["id"],
            now,
            selected["id"],
        ),
    )

    connection.execute(
        "UPDATE jobs SET status='processing' WHERE id=?",
        (task["job_id"],),
    )

    connection.execute(
        """
        INSERT INTO scheduler_decisions(
            task_id, job_id, chunk, profile, complexity, priority,
            mode, selected_worker, score, candidates, created
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            task["id"],
            task["job_id"],
            task["chunk"],
            task["profile"],
            task["complexity"],
            task["priority"],
            job_row["scheduler_mode"] if job_row else os.getenv("SCHEDULER_MODE", "intelligent"),
            selected["id"],
            selected["score"],
            json.dumps(candidates),
            now,
        ),
    )

    connection.commit()
    connection.close()

    return {
        "task": {
            "id": task["id"],
            "job_id": task["job_id"],
            "chunk": task["chunk"],
            "profile": task["profile"],
            "complexity": task["complexity"],
            "priority": task["priority"],
            "target_bitrate": task["target_bitrate"],
            "input": (
                f"/api/tasks/"
                f"{task['id']}/input"
            ),
        }
    }


# ============================================================
# TASK INPUT
# ============================================================

def mark_job_input_missing(connection, job_id, reason):
    now = time.time()
    connection.execute(
        """
        UPDATE workers
        SET status='idle', active=NULL, last_seen=?
        WHERE active IN (
            SELECT id FROM tasks
            WHERE job_id=? AND status NOT IN ('completed', 'cancelled', 'failed')
        )
        """,
        (now, job_id),
    )
    connection.execute(
        """
        UPDATE tasks
        SET status='failed', worker=NULL, error=?
        WHERE job_id=? AND status NOT IN ('completed', 'cancelled')
        """,
        (reason, job_id),
    )
    connection.execute(
        """
        UPDATE jobs
        SET status='failed', completed_at=?
        WHERE id=? AND status NOT IN ('completed', 'cancelled')
        """,
        (now, job_id),
    )


@app.get("/api/tasks/{tid}/status")
def task_status(tid: str):
    connection = db()
    task = connection.execute(
        """
        SELECT tasks.status, COALESCE(workers.disconnect_requested, 0) AS disconnect_requested
        FROM tasks
        LEFT JOIN workers ON workers.id=tasks.worker
        WHERE tasks.id=?
        """,
        (tid,),
    ).fetchone()
    connection.close()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "status": "cancelled" if task["disconnect_requested"] else task["status"]
    }

@app.get("/api/tasks/{tid}/input")
def task_input(tid: str):

    connection = db()

    task = connection.execute(
        """
        SELECT id, job_id, chunk_path, status, error
        FROM tasks
        WHERE id=?
        """,
        (tid,),
    ).fetchone()

    connection.close()

    if not task:

        raise HTTPException(
            status_code=404,
            detail="Task not found"
        )

    if task["status"] == "failed":
        raise HTTPException(
            status_code=410,
            detail=task["error"] or "Task input is no longer available",
        )

    path = Path(task["chunk_path"] or "")
    if not path.is_file():
        connection = db()
        reason = f"Input chunk is missing: {path}"
        mark_job_input_missing(connection, task["job_id"], reason)
        connection.commit()
        connection.close()
        raise HTTPException(status_code=410, detail=reason)

    return FileResponse(
        path,
        media_type="video/mp4"
    )


# ============================================================
# TASK COMPLETE
# ============================================================

@app.post(
    "/api/tasks/{tid}/complete"
)
async def complete(
    tid: str,
    output: UploadFile = File(...),
    download_time: float = Form(0),
    processing_time: float = Form(0),
    upload_time: float = Form(0),
):

    connection = db()

    task = connection.execute(
        "SELECT * FROM tasks WHERE id=?",
        (tid,),
    ).fetchone()

    if not task:

        connection.close()

        raise HTTPException(
            status_code=404,
            detail="Task not found"
        )

    job_state = connection.execute(
        "SELECT status FROM jobs WHERE id=?",
        (task["job_id"],),
    ).fetchone()
    if task["status"] in ("cancelled", "failed") or (job_state and job_state["status"] in ("cancelled", "failed")):
        connection.close()
        raise HTTPException(status_code=409, detail="Job is cancelled or failed")

    output_directory = (
        OUT
        / task["job_id"]
        / task["profile"]
    )

    output_directory.mkdir(
        parents=True,
        exist_ok=True
    )

    output_path = (
        output_directory
        / f"segment_{task['chunk']:05d}.ts"
    )

    with output_path.open("wb") as file:

        while True:

            data = await output.read(
                1024 * 1024
            )

            if not data:
                break

            file.write(data)

    # Mark task complete
    update_result = connection.execute(
        """
        UPDATE tasks
        SET
            status='completed',
            output=?,
            download_time=?,
            processing_time=?,
            upload_time=?,
            completed_at=?
        WHERE id=?
          AND status!='cancelled'
          AND EXISTS (SELECT 1 FROM jobs WHERE id=? AND status!='cancelled')
        """,
        (
            str(output_path),
            round(download_time, 4),
            round(processing_time, 4),
            round(upload_time, 4),
            time.time(),
            tid,
            task["job_id"],
        ),
    )
    if update_result.rowcount == 0:
        output_path.unlink(missing_ok=True)
        connection.close()
        raise HTTPException(status_code=409, detail="Job was cancelled")

    # Make worker available again
    if task["worker"]:

        connection.execute(
            """
            UPDATE workers
            SET
                status='idle',
                active=NULL,
                last_seen=?
            WHERE id=?
            """,
            (
                time.time(),
                task["worker"],
            ),
        )

        connection.execute(
            """
            UPDATE workers
            SET tasks_failed=tasks_failed+1
            WHERE id=?
            """,
            (task["worker"],),
        )

        connection.execute(
            """
            UPDATE workers
            SET tasks_completed=tasks_completed+1
            WHERE id=?
            """,
            (task["worker"],),
        )

    # Recalculate completed tasks
    connection.execute(
        """
        UPDATE jobs
        SET done=(
            SELECT COUNT(*)
            FROM tasks
            WHERE job_id=?
            AND status='completed'
        )
        WHERE id=?
        """,
        (
            task["job_id"],
            task["job_id"],
        ),
    )

    job_row = connection.execute(
        "SELECT * FROM jobs WHERE id=?",
        (task["job_id"],),
    ).fetchone()

    if (
        job_row
        and job_row["status"] != "cancelled"
        and job_row["done"] == job_row["total"]
    ):

        connection.execute(
            """
            UPDATE jobs
            SET status='completed',
                completed_at=?
            WHERE id=? AND status!='cancelled'
            """,
            (time.time(), task["job_id"]),
        )

    connection.commit()
    connection.close()

    await make_hls(
        task["job_id"]
    )

    return {
        "ok": True
    }


# ============================================================
# TASK FAILURE
# ============================================================

@app.post(
    "/api/tasks/{tid}/fail"
)
async def fail(
    tid: str,
    request: Request,
):

    try:

        data = await request.json()

    except Exception:

        data = {}

    connection = db()

    task = connection.execute(
        "SELECT * FROM tasks WHERE id=?",
        (tid,),
    ).fetchone()

    if task:

        if task["status"] in ("cancelled", "failed"):
            connection.close()
            return {"ok": True, "status": task["status"]}

        connection.execute(
            """
            UPDATE tasks
            SET
                status='queued',
                worker=NULL,
                error=?
            WHERE id=?
            """,
            (
                data.get(
                    "error",
                    "failed"
                ),
                tid,
            ),
        )

        if task["worker"]:

            connection.execute(
                """
                UPDATE workers
                SET
                    status='idle',
                    active=NULL,
                    last_seen=?
                WHERE id=?
                """,
                (
                    time.time(),
                    task["worker"],
                ),
            )

            connection.execute(
                """
                UPDATE workers
                SET tasks_failed=tasks_failed+1
                WHERE id=?
                """,
                (task["worker"],),
            )

        connection.commit()

    connection.close()

    return {
        "ok": True
    }


# ============================================================
# HLS GENERATION
# ============================================================

async def make_hls(jid: str):

    connection = db()

    job_row = connection.execute(
        "SELECT * FROM jobs WHERE id=?",
        (jid,),
    ).fetchone()

    if (
        not job_row
        or job_row["status"] == "cancelled"
        or job_row["done"]
        < job_row["total"]
    ):

        connection.close()
        return

    profiles = json.loads(
        job_row["profiles"]
    )

    for profile in profiles:

        rows = connection.execute(
            """
            SELECT chunk, output
            FROM tasks
            WHERE job_id=?
            AND profile=?
            AND status='completed'
            ORDER BY chunk
            """,
            (
                jid,
                profile,
            ),
        ).fetchall()

        playlist_path = (
            OUT
            / jid
            / profile
            / "index.m3u8"
        )

        playlist_path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        playlist = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            "#EXT-X-TARGETDURATION:7\n"
            "#EXT-X-MEDIA-SEQUENCE:0\n"
        )

        # IMPORTANT:
        # The playlist lives at:
        #
        #   /api/jobs/{jid}/playlist/{profile}
        #
        # Therefore the segment path must include
        # the profile directory.
        #
        # Example:
        #
        #   1080p/segment_00000.ts
        #
        # This resolves to:
        #
        #   /api/jobs/{jid}/playlist/1080p/segment_00000.ts
        #
        # which matches the segment route below.

        for row in rows:

            filename = Path(
                row["output"]
            ).name

            playlist += (
                "#EXTINF:6.000,\n"
                f"{profile}/{filename}\n"
            )

        playlist += (
            "#EXT-X-ENDLIST\n"
        )

        playlist_path.write_text(
            playlist
        )

    master_path = (
        OUT
        / jid
        / "master.m3u8"
    )
    bandwidth = {
        "360p": "800000",
        "480p": "1500000",
        "720p": "3000000",
        "1080p": "5000000",
    }
    resolution = {
        "360p": "640x360",
        "480p": "854x480",
        "720p": "1280x720",
        "1080p": "1920x1080",
    }
    master = "#EXTM3U\n#EXT-X-VERSION:3\n"
    for profile in profiles:
        master += (
            f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth.get(profile, '1000000')},"
            f"RESOLUTION={resolution.get(profile, '1280x720')}\n"
            f"{profile}/index.m3u8\n"
        )
    master_path.write_text(master)

    connection.close()


# ============================================================
# HLS PLAYLIST
# ============================================================

@app.get(
    "/api/jobs/{jid}/playlist/{prof}"
)
def playlist(
    jid: str,
    prof: str,
):

    path = (
        OUT
        / jid
        / prof
        / "index.m3u8"
    )

    if not path.exists():

        raise HTTPException(
            status_code=404,
            detail="Not ready"
        )

    return FileResponse(
        path,
        media_type=(
            "application/vnd.apple.mpegurl"
        ),
    )


@app.get("/api/jobs/{jid}/master.m3u8")
def master_playlist(jid: str):
    path = OUT / jid / "master.m3u8"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not ready")
    return FileResponse(
        path,
        media_type="application/vnd.apple.mpegurl",
    )


# ============================================================
# HLS SEGMENT
# ============================================================

@app.get(
    "/api/jobs/{jid}/playlist/"
    "{prof}/{filename}"
)
def segment(
    jid: str,
    prof: str,
    filename: str,
):

    path = (
        OUT
        / jid
        / prof
        / filename
    )

    if not path.exists():

        raise HTTPException(
            status_code=404,
            detail="Not found"
        )

    return FileResponse(
        path,
        media_type="video/mp2t"
    )


# ============================================================
# REAPER / FAULT RECOVERY
# ============================================================

async def reaper():

    while True:

        await asyncio.sleep(15)

        connection = db()

        cutoff = (
            time.time() - 45
        )

        stale_tasks = connection.execute(
            """
            SELECT tasks.id, tasks.worker
            FROM tasks
            JOIN jobs ON jobs.id=tasks.job_id
            WHERE tasks.status IN ('processing','assigned','downloading','transcoding','uploading')
              AND tasks.started<?
              AND jobs.status IN ('queued', 'processing')
            """,
            (cutoff,),
        ).fetchall()

        for task in stale_tasks:

            # Requeue task
            connection.execute(
                """
                UPDATE tasks
                SET
                    status='queued',
                    worker=NULL,
                    error='timeout; requeued'
                WHERE id=?
                  AND status IN ('processing','assigned','downloading','transcoding','uploading')
                  AND EXISTS (
                      SELECT 1 FROM jobs
                      WHERE jobs.id=tasks.job_id
                        AND jobs.status IN ('queued', 'processing')
                  )
                """,
                (task["id"],),
            )

            # Free worker
            if task["worker"]:

                connection.execute(
                    """
                    UPDATE workers
                    SET
                        status='idle',
                        active=NULL
                    WHERE id=?
                    """,
                    (task["worker"],),
                )

                connection.execute(
                    """
                    UPDATE workers
                    SET tasks_recovered=tasks_recovered+1
                    WHERE id=?
                    """,
                    (task["worker"],),
                )

        connection.commit()
        connection.close()
