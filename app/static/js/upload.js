import { state } from "./state.js";
import { startJobPolling } from "./jobs.js";
import { fetchJson } from "./utils.js";

function initQualitySelection() {
    document.querySelectorAll("[data-q]").forEach(button => {
        button.addEventListener("click", () => {
            const profile = button.dataset.q;
            if (state.selectedProfiles.has(profile)) {
                if (state.selectedProfiles.size === 1) {
                    return;
                }
                state.selectedProfiles.delete(profile);
            } else {
                state.selectedProfiles.add(profile);
            }
            button.classList.toggle("on", state.selectedProfiles.has(profile));
            document.getElementById("sel").textContent =
                [...state.selectedProfiles].join(" · ");
        });
    });
}

async function startProcessing() {
    const fileInput = document.getElementById("file");
    const button = document.getElementById("go");
    if (!fileInput.files[0]) {
        alert("Choose a video");
        return;
    }
    button.disabled = true;
    button.textContent = "Uploading + chunking…";

    try {
        const form = new FormData();
        form.append("video", fileInput.files[0]);
        form.append("profiles", [...state.selectedProfiles].join(","));
        const data = await fetchJson("/api/upload", { method: "POST", body: form });
        state.jobId = data.job_id;
        document.getElementById("job").style.display = "block";
        document.getElementById("jt").textContent = `Processing · ${data.job_id.slice(0, 8)}`;
        button.textContent = "Distributed processing started";
        startJobPolling(data.job_id);
    } catch (error) {
        console.error(error);
        button.disabled = false;
        button.textContent = "Start distributed processing";
        alert(error.message);
    }
}

export function initUpload() {
    const fileInput = document.getElementById("file");
    fileInput.addEventListener("change", () => {
        if (fileInput.files[0]) {
            document.getElementById("fn").textContent = fileInput.files[0].name;
        }
    });
    document.getElementById("go").addEventListener("click", startProcessing);
    initQualitySelection();
}
