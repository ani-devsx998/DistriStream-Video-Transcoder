import argparse
import socket
import time
import uuid
import subprocess
import tempfile

from pathlib import Path

import requests
import psutil


# ============================================================
# ARGUMENTS
# ============================================================

parser = argparse.ArgumentParser()

parser.add_argument(
    "--coordinator",
    required=True
)

parser.add_argument(
    "--name"
)

args = parser.parse_args()


# ============================================================
# WORKER IDENTITY
# ============================================================

BASE = args.coordinator.rstrip("/")

NAME = (
    args.name
    or socket.gethostname()
)

identity_directory = (
    Path(tempfile.gettempdir())
    / "dv_worker_identity"
)
identity_directory.mkdir(
    parents=True,
    exist_ok=True
)
identity_file = (
    identity_directory
    / (
        "".join(
            char if char.isalnum() or char in "-_" else "_"
            for char in NAME
        )
        + ".id"
    )
)

if identity_file.exists():
    WID = identity_file.read_text().strip()
else:
    WID = str(uuid.uuid4())
    identity_file.write_text(WID)

WORK_DIR = (
    Path(tempfile.gettempdir())
    / ("dv_worker_" + WID)
)

WORK_DIR.mkdir(
    parents=True,
    exist_ok=True
)


# ============================================================
# HTTP HELPER
# ============================================================

def post(
    path,
    data=None,
    files=None
):
    return requests.post(
        BASE + path,
        json=data,
        files=files,
        timeout=180
    )


class TaskCancelled(Exception):
    pass


def task_is_cancelled(task_id):
    try:
        response = requests.get(
            BASE + f"/api/tasks/{task_id}/status",
            timeout=3,
        )
        if response.status_code == 404:
            # Stopped jobs delete their task rows; a missing task is terminal too.
            return True
        response.raise_for_status()
        return response.json().get("status") in ("cancelled", "failed")
    except Exception:
        return False


def run_ffmpeg(command, task_id):
    if task_is_cancelled(task_id):
        raise TaskCancelled("Job was cancelled")
    started = time.time()
    with tempfile.TemporaryFile() as error_log:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=error_log,
        )
        while process.poll() is None:
            if task_is_cancelled(task_id):
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise TaskCancelled("Job was cancelled")
            time.sleep(0.5)

        error_log.seek(0)
        error = error_log.read().decode(errors="ignore")[-1200:]
    if process.returncode != 0:
        raise RuntimeError("FFmpeg failed:\n" + error)
    return time.time() - started


# ============================================================
# GPU DETECTION
# ============================================================

def detect_gpu():
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name",
                "--format=csv,noheader",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            text=True,
        )
        if result.returncode == 0:
            name = result.stdout.strip().splitlines()
            if name:
                return name[0]
    except Exception:
        pass
    return "FFmpeg software encoder"


# ============================================================
# HEARTBEAT
# ============================================================

def heartbeat(
    status="idle",
    active=None
):

    try:

        response = post(
            f"/api/workers/{WID}/heartbeat",
            {
                "status": status,
                "active": active,
                "cpu_usage": psutil.cpu_percent(interval=None),
                "gpu_usage": 0,
                "ram": psutil.virtual_memory().total / (1024 ** 3),
                "queue_depth": 1 if active else 0,
                "network_cost": 0,
                "gpu": detect_gpu(),
            }
        )
        response.raise_for_status()
        return response.json().get("disconnect", False)

    except Exception:
        return False


# ============================================================
# REGISTER WORKER
# ============================================================

post(
    "/api/workers/register",
    {
        "id": WID,
        "name": NAME,
        "host": socket.gethostname(),
        "cpu": psutil.cpu_count() or 1,
        "gpu": detect_gpu(),
        "ram": psutil.virtual_memory().total / (1024 ** 3),
        "cpu_usage": psutil.cpu_percent(interval=None),
        "gpu_usage": 0,
        "queue_depth": 0,
        "max_queue": 5,
        "network_cost": 0,
    }
).raise_for_status()


print(
    f"{NAME} registered -> {BASE}"
)


# ============================================================
# VIDEO PROFILES
# ============================================================

RATES = {

    "1080p": (
        "1920:1080",
        "5000k"
    ),

    "720p": (
        "1280:720",
        "3000k"
    ),

    "480p": (
        "854:480",
        "1500k"
    ),

    "360p": (
        "640:360",
        "800k"
    ),
}


# ============================================================
# MAIN WORKER LOOP
# ============================================================

while True:

    task = None

    try:

        # ----------------------------------------------------
        # ASK COORDINATOR FOR A TASK
        # ----------------------------------------------------

        response = post(
            "/api/tasks/claim",
            {
                "wid": WID
            }
        )

        response.raise_for_status()

        task = response.json()["task"]

        # No task available
        if not task:

            if heartbeat("idle"):
                print(f"[{NAME}] disconnected by coordinator")
                break

            time.sleep(1)

            continue

        print(
            f"[{NAME}] "
            f"received task "
            f"{task['id']} | "
            f"chunk={task['chunk']} | "
            f"profile={task['profile']}"
        )


        # ----------------------------------------------------
        # MARK WORKER BUSY
        # ----------------------------------------------------

        if heartbeat("busy", task["id"]):
            print(f"[{NAME}] disconnected by coordinator")
            break


        # ----------------------------------------------------
        # LOCAL FILES
        # ----------------------------------------------------

        input_file = (
            WORK_DIR
            / f"{task['id']}.mp4"
        )

        output_file = (
            WORK_DIR
            / f"{task['id']}.ts"
        )


        # ----------------------------------------------------
        # PROFILE
        # ----------------------------------------------------

        scale, bitrate = RATES[
            task["profile"]
        ]

        if task.get("target_bitrate"):
            bitrate = f"{task['target_bitrate']}k"


        # ----------------------------------------------------
        # DOWNLOAD CHUNK
        # ----------------------------------------------------

        print(
            f"[{NAME}] "
            f"Downloading chunk "
            f"{task['chunk']}"
        )

        download_started = time.time()
        with requests.get(
            BASE + task["input"],
            stream=True,
            timeout=180
        ) as response:

            response.raise_for_status()

            with input_file.open(
                "wb"
            ) as file:

                for block in response.iter_content(
                    1024 * 1024
                ):

                    if task_is_cancelled(task["id"]):
                        raise TaskCancelled("Job was cancelled")

                    if block:
                        file.write(block)
        download_time = time.time() - download_started


        # ----------------------------------------------------
        # RUN FFMPEG
        # ----------------------------------------------------

        print(
            f"[{NAME}] "
            f"Processing "
            f"{task['profile']}"
        )

        command = [
            "ffmpeg",
            "-y",

            "-i",
            str(input_file),

            "-vf",
            f"scale={scale}:force_original_aspect_ratio=decrease,"
            "pad=ceil(iw/2)*2:ceil(ih/2)*2",

            "-c:v",
            "libx264",

            "-preset",
            "veryfast",

            "-b:v",
            bitrate,

            "-g",
            "48",

            "-keyint_min",
            "48",

            "-sc_threshold",
            "0",

            "-c:a",
            "aac",

            "-b:a",
            "128k",

            "-f",
            "mpegts",

            str(output_file)
        ]


        processing_time = run_ffmpeg(command, task["id"])


        # ----------------------------------------------------
        # UPLOAD RESULT
        # ----------------------------------------------------

        print(
            f"[{NAME}] "
            f"Uploading result"
        )

        if task_is_cancelled(task["id"]):
            raise TaskCancelled("Job was cancelled")

        upload_started = time.time()
        with output_file.open(
            "rb"
        ) as file:

            requests.post(
                BASE
                + f"/api/tasks/"
                f"{task['id']}"
                "/complete",

                files={
                    "output": (
                        "segment.ts",
                        file,
                        "video/mp2t"
                    )
                },
                data={
                    "download_time": round(download_time, 4),
                    "processing_time": round(processing_time, 4),
                    "upload_time": 0,
                },

                timeout=300
            ).raise_for_status()
        upload_time = time.time() - upload_started


        # ----------------------------------------------------
        # CLEAN LOCAL FILES
        # ----------------------------------------------------

        input_file.unlink(
            missing_ok=True
        )

        output_file.unlink(
            missing_ok=True
        )


        print(
            f"[{NAME}] "
            f"COMPLETED "
            f"chunk={task['chunk']} "
            f"profile={task['profile']}"
        )


        # ----------------------------------------------------
        # READY FOR NEXT TASK
        # ----------------------------------------------------

        if heartbeat("idle"):
            print(f"[{NAME}] disconnected by coordinator")
            break


    except TaskCancelled:
        print(f"[{NAME}] task cancelled; stopping this worker task")
        for local_path in (locals().get("input_file"), locals().get("output_file")):
            if local_path:
                local_path.unlink(missing_ok=True)
        if heartbeat("idle"):
            print(f"[{NAME}] disconnected by coordinator")
            break

    except Exception as error:

        print(
            f"[{NAME}] ERROR: "
            f"{error}"
        )

        # Only fail a task if we actually
        # successfully claimed one.
        if task is not None:

            try:

                post(
                    f"/api/tasks/"
                    f"{task['id']}"
                    "/fail",

                    {
                        "error": str(error)
                    }
                )

            except Exception as fail_error:

                print(
                    f"[{NAME}] "
                    f"Could not report failure: "
                    f"{fail_error}"
                )

        if heartbeat("idle"):
            print(f"[{NAME}] disconnected by coordinator")
            break

        time.sleep(2)
