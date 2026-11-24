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

WID = str(uuid.uuid4())

NAME = (
    args.name
    or socket.gethostname()
)

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


# ============================================================
# HEARTBEAT
# ============================================================

def heartbeat(
    status="idle",
    active=None
):

    try:

        post(
            f"/api/workers/{WID}/heartbeat",
            {
                "status": status,
                "active": active
            }
        )

    except Exception:
        pass


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
        "gpu": "FFmpeg software encoder"
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

            heartbeat("idle")

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

        heartbeat(
            "busy",
            task["id"]
        )


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


        # ----------------------------------------------------
        # DOWNLOAD CHUNK
        # ----------------------------------------------------

        print(
            f"[{NAME}] "
            f"Downloading chunk "
            f"{task['chunk']}"
        )

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

                    if block:
                        file.write(block)


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


        process = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE
        )


        # ----------------------------------------------------
        # FFMPEG FAILURE
        # ----------------------------------------------------

        if process.returncode != 0:

            error = (
                process.stderr
                .decode(
                    errors="ignore"
                )[-1200:]
            )

            raise RuntimeError(
                "FFmpeg failed:\n"
                + error
            )


        # ----------------------------------------------------
        # UPLOAD RESULT
        # ----------------------------------------------------

        print(
            f"[{NAME}] "
            f"Uploading result"
        )

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

                timeout=300
            ).raise_for_status()


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

        heartbeat("idle")


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

        heartbeat("idle")

        time.sleep(2)