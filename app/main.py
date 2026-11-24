import asyncio
import json
import sqlite3
import subprocess
import time
import uuid

from pathlib import Path

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
        """
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
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,
            host=excluded.host,
            status='idle',
            last_seen=excluded.last_seen,
            cpu=excluded.cpu,
            gpu=excluded.gpu
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
            data.get(
                "gpu",
                "unknown"
            ),
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

    connection.execute(
        """
        UPDATE workers
        SET
            status=?,
            last_seen=?,
            active=?
        WHERE id=?
        """,
        (
            data.get(
                "status",
                "idle"
            ),
            time.time(),
            data.get("active"),
            wid,
        ),
    )

    connection.commit()
    connection.close()

    return {
        "ok": True
    }


# ============================================================
# VIDEO JOB
# ============================================================

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
            error
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


# ============================================================
# UPLOAD + CHUNKING
# ============================================================

@app.post("/api/upload")
async def upload(
    video: UploadFile = File(...),
    profiles: str = Form(
        "1080p,720p,480p"
    ),
):

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

    # Create chunk directory
    chunk_directory = CH / job_id

    chunk_directory.mkdir(
        parents=True,
        exist_ok=True
    )

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

    if not chunks:

        raise HTTPException(
            status_code=500,
            detail="No chunks created"
        )

    total_tasks = (
        len(chunks)
        * len(selected_profiles)
    )

    connection = db()

    connection.execute(
        """
        INSERT INTO jobs
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    profile
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(
                        uuid.uuid4()
                    ),
                    job_id,
                    chunk_index,
                    str(chunk_path),
                    profile,
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
        SELECT *
        FROM tasks
        WHERE status='queued'
        ORDER BY attempts ASC, chunk ASC
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

    # Atomically assign the task
    connection.execute(
        """
        UPDATE tasks
        SET
            status='processing',
            worker=?,
            attempts=attempts+1,
            started=?
        WHERE id=?
        """,
        (
            worker_id,
            time.time(),
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
            time.time(),
            worker_id,
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
            "input": (
                f"/api/tasks/"
                f"{task['id']}/input"
            ),
        }
    }


# ============================================================
# TASK INPUT
# ============================================================

@app.get("/api/tasks/{tid}/input")
def task_input(tid: str):

    connection = db()

    task = connection.execute(
        """
        SELECT chunk_path
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

    path = Path(
        task["chunk_path"]
    )

    if not path.exists():

        raise HTTPException(
            status_code=404,
            detail="Chunk file not found"
        )

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
    connection.execute(
        """
        UPDATE tasks
        SET
            status='completed',
            output=?
        WHERE id=?
        """,
        (
            str(output_path),
            tid,
        ),
    )

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
        and job_row["done"]
        == job_row["total"]
    ):

        connection.execute(
            """
            UPDATE jobs
            SET status='completed'
            WHERE id=?
            """,
            (task["job_id"],),
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
            SELECT id, worker
            FROM tasks
            WHERE status='processing'
            AND started<?
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

        connection.commit()
        connection.close()