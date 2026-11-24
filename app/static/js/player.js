import { state } from "./state.js";
import { escapeHtml } from "./utils.js";

let hlsPlayer = null;

export function renderOutputs(job) {
    const output = document.getElementById("out");
    if (output.dataset.rendered === "true") {
        return;
    }
    output.dataset.rendered = "true";
    output.innerHTML = `
        <h3 style="margin-top:22px">Outputs</h3>
        <div class="outputs">
            ${job.profiles.map(profile => `
                <button class="output" type="button" data-play-profile="${escapeHtml(profile)}">
                    <strong>${escapeHtml(profile)} output</strong>
                    <small>Watch generated stream</small>
                </button>
            `).join("")}
        </div>`;
    document.getElementById("videoPlayer").style.display = "block";

    const controls = document.getElementById("qualityControls");
    controls.innerHTML = job.profiles.map(profile => `
        <button class="player-quality" type="button" data-quality="${escapeHtml(profile)}">
            ${escapeHtml(profile)}
        </button>
    `).join("");
    controls.querySelectorAll("[data-quality]").forEach(button => {
        button.addEventListener("click", () => loadVideo(button.dataset.quality));
    });
    output.querySelectorAll("[data-play-profile]").forEach(button => {
        button.addEventListener("click", () => loadVideo(button.dataset.playProfile));
    });
    loadVideo(job.profiles[0]);
}

export function loadVideo(profile) {
    if (!state.jobId) {
        return;
    }
    const video = document.getElementById("video");
    document.getElementById("playerQuality").textContent = profile;
    document.querySelectorAll(".player-quality").forEach(button => {
        button.classList.toggle("active", button.dataset.quality === profile);
    });

    if (hlsPlayer) {
        hlsPlayer.destroy();
        hlsPlayer = null;
    }
    const playlist = `/api/jobs/${state.jobId}/playlist/${encodeURIComponent(profile)}`;
    const Hls = window.Hls;
    if (Hls && Hls.isSupported()) {
        hlsPlayer = new Hls();
        hlsPlayer.loadSource(playlist);
        hlsPlayer.attachMedia(video);
        hlsPlayer.on(Hls.Events.MANIFEST_PARSED, () => {
            video.play().catch(() => {});
        });
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
        video.src = playlist;
        video.play().catch(() => {});
    } else {
        alert("This browser does not support HLS playback.");
    }
}
