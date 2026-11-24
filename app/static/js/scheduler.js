import { state } from "./state.js";
import { escapeHtml, fetchJson } from "./utils.js";

export async function refreshScheduler() {
    try {
        const [status, decisions] = await Promise.all([
            fetchJson("/api/scheduler/status"),
            fetchJson("/api/scheduler/decisions?limit=12"),
        ]);
        document.getElementById("schedulerMode").textContent =
            status.mode === "intelligent" ? "Intelligent mode" : "Baseline mode";
        document.getElementById("schedulerStatus").innerHTML =
            `<b>${escapeHtml(status.mode)}</b> · ${status.queued_tasks} queued · ` +
            `${status.active_tasks} active`;
        document.getElementById("schedulerDecisions").innerHTML =
            decisions.length
                ? decisions.map(decision => `
                    <div style="padding:8px 0;border-bottom:1px solid #263653">
                        <b>${escapeHtml(decision.profile)}</b> · chunk ${Number(decision.chunk) + 1}
                        assigned to ${escapeHtml(decision.selected_worker)}
                    </div>
                `).join("")
                : `<span class="muted">Scheduling assignments will appear here.</span>`;

        if (state.jobId) {
            const metrics = await fetchJson(`/api/jobs/${state.jobId}/metrics`);
            document.getElementById("jobMetrics").innerHTML = `
                <div class="stats" style="grid-template-columns:repeat(4,1fr)">
                    <div class="stat"><div class="stat-label">Upload</div><b>${escapeHtml(metrics.upload_time ?? "-")} s</b></div>
                    <div class="stat"><div class="stat-label">Chunking</div><b>${escapeHtml(metrics.chunking_time ?? "-")} s</b></div>
                    <div class="stat"><div class="stat-label">Analysis</div><b>${escapeHtml(metrics.analysis_time ?? "-")} s</b></div>
                    <div class="stat"><div class="stat-label">Total</div><b>${escapeHtml(metrics.total_job_time ?? "-")} s</b></div>
                </div>`;
        }
    } catch (error) {
        console.error("Scheduler refresh error:", error);
    }
}

export function initScheduler() {
    document.getElementById("schedulerMode").addEventListener("click", async event => {
        const current = event.currentTarget.textContent;
        const mode = current.includes("Intelligent") ? "baseline" : "intelligent";
        try {
            await fetchJson("/api/scheduler/mode", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ mode }),
            });
            await refreshScheduler();
        } catch (error) {
            alert(error.message);
        }
    });
    refreshScheduler();
    setInterval(refreshScheduler, 3000);
}
