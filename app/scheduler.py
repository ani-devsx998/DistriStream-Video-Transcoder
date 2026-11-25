import time
import os
import json


SCHEDULER_MODE = "intelligent"
QUEUE_CAPACITY = 5


def priority_for(complexity):
    if complexity < 0.30:
        return 0.25
    if complexity < 0.60:
        return 0.50
    if complexity < 0.80:
        return 0.75
    return 1.00


def get_workers(connection):
    cutoff = time.time() - 20
    return connection.execute(
        """
        SELECT *
        FROM workers
        WHERE last_seen >= ?
        AND status != 'offline'
        """,
        (cutoff,),
    ).fetchall()


def queue_depth(connection, worker_id):
    return connection.execute(
        """
        SELECT COUNT(*) AS count
        FROM tasks
        WHERE worker=?
        AND status IN ('assigned', 'downloading', 'transcoding', 'uploading', 'processing')
        """,
        (worker_id,),
    ).fetchone()["count"]


def choose_worker(connection, task):
    workers = get_workers(connection)
    eligible = []

    job = connection.execute(
        "SELECT scheduler_mode, manual_workers FROM jobs WHERE id=?",
        (task["job_id"],),
    ).fetchone()
    mode = (job["scheduler_mode"] if job else None) or os.getenv("SCHEDULER_MODE", SCHEDULER_MODE)
    if mode == "manual":
        selected_workers = set(json.loads((job["manual_workers"] if job else None) or "[]"))
        workers = [
            worker for worker in workers
            if worker["id"] in selected_workers or worker["name"] in selected_workers
        ]

    if not workers:
        return None, []

    max_cores = max(
        1,
        max(worker["cpu"] or 1 for worker in workers),
    )

    for worker in workers:
        active = queue_depth(connection, worker["id"])
        capacity = (worker["cpu"] or 1) / max_cores
        load = min(1.0, (worker["cpu_usage"] or 0) / 100)
        queue = active / QUEUE_CAPACITY
        network = min(1.0, (worker["network_cost"] or 0.0))

        if active >= QUEUE_CAPACITY:
            continue

        if mode in ("baseline", "manual"):
            score = 1.0 / (1 + active)
        else:
            score = (
                0.25 * (task["priority"] or priority_for(task["complexity"] or 0))
                + 0.30 * capacity
                - 0.20 * load
                - 0.15 * queue
                - 0.10 * network
            )

        eligible.append({
            "id": worker["id"],
            "name": worker["name"],
            "score": round(score, 4),
            "capacity": round(capacity, 4),
            "load": round(load, 4),
            "queue": round(queue, 4),
            "network": round(network, 4),
            "active": active,
        })

    if not eligible:
        return None, []

    chosen = max(eligible, key=lambda row: row["score"])
    return chosen, eligible
