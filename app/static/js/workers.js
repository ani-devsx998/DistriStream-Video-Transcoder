import { escapeHtml, fetchJson } from "./utils.js";

export function initWorkers() {
    async function refreshWorkers() {
        try {
            const entries = await fetchJson("/api/workers");
            const byName = new Map();
            entries.forEach(worker => {
                const name = worker.name || worker.id || "Unknown worker";
                const current = byName.get(name);
                if (!current || (worker.last_seen || 0) > (current.last_seen || 0)) {
                    byName.set(name, worker);
                }
            });
            const workers = [...byName.values()].sort((a, b) => {
                if ((a.status === "busy") !== (b.status === "busy")) {
                    return a.status === "busy" ? -1 : 1;
                }
                return (b.last_seen || 0) - (a.last_seen || 0);
            });

            document.getElementById("workerCount").textContent =
                `${workers.length} worker${workers.length === 1 ? "" : "s"} online`;
            document.getElementById("workers").innerHTML = workers.length
                ? workers.map((worker, index) => {
                    const busy = worker.status === "busy";
                    const cpuUsage = worker.cpu_usage == null ? "-" : `${worker.cpu_usage}%`;
                    const ram = worker.ram == null ? "-" : `${Number(worker.ram).toFixed(1)} GB`;
                    const queue = worker.queue_depth == null ? "-" : worker.queue_depth;
                    return `
                        <div class="worker ${busy ? "busy" : ""}">
                            <div class="worker-head">
                                <div class="worker-name">${escapeHtml(worker.name || `Worker ${index + 1}`)}</div>
                                <div class="badge">${busy ? "PROCESSING" : "READY"}</div>
                            </div>
                            <div class="worker-meta">
                                <div class="meta"><span>CPU CORES</span><b>${escapeHtml(worker.cpu || "-")}</b></div>
                                <div class="meta"><span>ENGINE</span><b>${escapeHtml(worker.gpu || "FFmpeg")}</b></div>
                                <div class="meta"><span>CPU LOAD</span><b>${escapeHtml(cpuUsage)}</b></div>
                                <div class="meta"><span>RAM</span><b>${escapeHtml(ram)}</b></div>
                                <div class="meta"><span>QUEUE</span><b>${escapeHtml(queue)} / 5</b></div>
                            </div>
                            <div class="activity">${busy
                                ? `Processing task ${escapeHtml(worker.active || "")}`
                                : "Ready for next task"}</div>
                        </div>`;
                }).join("")
                : `<div class="empty">No workers registered yet.</div>`;
        } catch (error) {
            console.error("Worker refresh error:", error);
        }
    }

    refreshWorkers();
    setInterval(refreshWorkers, 1500);
}
