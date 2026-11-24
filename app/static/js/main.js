import { initUpload } from "./upload.js";
import { initScheduler } from "./scheduler.js";
import { initWorkers } from "./workers.js";
import { initWorkerOnboarding } from "./worker_onboarding.js";

initUpload();
initScheduler();
initWorkers();
initWorkerOnboarding();
