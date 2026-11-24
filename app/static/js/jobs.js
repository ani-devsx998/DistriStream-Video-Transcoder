import { state } from "./state.js";
import { fetchJson } from "./utils.js";
import { renderOutputs } from "./player.js";

let pollTimer = null;

export function startJobPolling(jobId) {
    state.jobId = jobId;
    if (pollTimer) {
        clearTimeout(pollTimer);
    }
    poll();
}

async function poll() {
    if (!state.jobId) {
        return;
    }
    try {
        const job = await fetchJson(`/api/jobs/${state.jobId}`);
        const chunkCount = job.profiles.length
            ? Math.round(job.total / job.profiles.length)
            : 0;
        document.getElementById("chunks").textContent = chunkCount;
        document.getElementById("tasks").textContent = job.total;
        document.getElementById("done").textContent = job.done;
        document.getElementById("st").textContent = job.status;

        const percentage = job.total ? Math.round(job.done / job.total * 100) : 0;
        document.getElementById("fill").style.width = `${percentage}%`;
        document.getElementById("pct").textContent = `${percentage}%`;

        if (job.status === "completed") {
            document.getElementById("msg").textContent =
                "All distributed tasks completed · output ready";
            document.getElementById("go").textContent = "✓ Processing complete";
            renderOutputs(job);
        } else {
            document.getElementById("msg").textContent =
                `${percentage}% complete · ${job.done} / ${job.total} tasks`;
            document.getElementById("go").textContent =
                job.status === "processing"
                    ? "Workers processing in parallel…"
                    : "Waiting for worker nodes…";
        }
    } catch (error) {
        console.error("Job polling error:", error);
    }
    pollTimer = setTimeout(poll, 1500);
}
