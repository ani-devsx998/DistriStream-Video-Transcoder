export function initWorkerOnboarding() {
    const address = document.getElementById("coordinatorAddress");
    const command = document.getElementById("workerCommand");
    const message = document.getElementById("workerCommandMessage");
    const copyButton = document.getElementById("copyWorkerCommand");

    function renderCommand() {
        const value = address.value.trim().replace(/\/+$/, "");
        let parsed;
        try {
            parsed = new URL(value);
        } catch {
            command.textContent = "Enter this coordinator's LAN address, e.g. http://192.168.1.10:8000";
            copyButton.disabled = true;
            return;
        }
        if (!["http:", "https:"].includes(parsed.protocol)) {
            command.textContent = "Coordinator address must start with http:// or https://";
            copyButton.disabled = true;
            return;
        }
        command.textContent =
            `cd <project-folder>\\worker\n` +
            `.\\.venv\\Scripts\\Activate.ps1\n` +
            `python worker.py --coordinator ${value} --name Friend-Laptop`;
        copyButton.disabled = false;
    }

    address.addEventListener("input", renderCommand);
    copyButton.addEventListener("click", async () => {
        try {
            await navigator.clipboard.writeText(command.textContent);
            message.textContent = "Worker command copied.";
        } catch (error) {
            message.textContent = "Clipboard access failed; select and copy the command.";
            console.error("Could not copy worker command:", error);
        }
    });
    copyButton.disabled = true;
    renderCommand();
}
