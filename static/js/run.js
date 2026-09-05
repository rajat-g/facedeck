/* Run / cancel / poll processing status.
   While running, people stream in via checkpoint versions (see groups.js);
   curation stays locked until the run finishes (see ops.js). */

import { $, api, saveSettings, state } from "./core.js";
import { toastError } from "./toast.js";
import { refreshPeopleList } from "./groups.js";

let refresh = () => {};
let lastGroupsVersion = -1;

export function initRun(loadGroups) {
    refresh = loadGroups;
    $("#run-btn").addEventListener("click", startRun);
    $("#cancel-btn").addEventListener("click", cancelRun);
}

async function startRun() {
    const folders = $("#input-folders").value.split("\n").map((l) => l.trim()).filter(Boolean);
    if (folders.length === 0) { toastError("Add at least one input folder."); return; }

    try {
        await api("/api/run", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                input_folders: folders,
                output_faces: $("#output-faces").value.trim(),
                db_file: $("#db-file").value.trim(),
                threshold: parseFloat($("#threshold").value),
            }),
        });
    } catch (err) { toastError(err.message); return; }

    saveSettings();
    $("#run-btn").disabled = true;
    $("#cancel-btn").classList.remove("hidden");
    $("#status-panel").classList.remove("hidden");
    $("#status-meta").textContent = `${folders.length} folder(s)`;
    document.body.classList.add("is-running");
    lastGroupsVersion = -1;
    state.pollTimer = setInterval(pollStatus, 1000);
    pollStatus();
}

async function cancelRun() {
    try { await api("/api/cancel", { method: "POST" }); }
    catch (err) { toastError(err.message); }
}

async function pollStatus() {
    let status;
    try { status = await api("/api/status"); } catch (_) { return; }

    const pct = status.total > 0 ? (status.processed / status.total) * 100 : 0;
    $("#progress-bar").style.width = `${pct}%`;
    $("#progress-text").textContent = status.total > 0 ? `${status.processed} / ${status.total}` : "Working…";
    $("#log-output").textContent = (status.log || []).join("\n");
    $("#log-output").scrollTop = $("#log-output").scrollHeight;

    const errorBox = $("#error-box");
    if (status.error) { errorBox.textContent = status.error; errorBox.classList.remove("hidden"); }
    else errorBox.classList.add("hidden");

    // Streaming: a new checkpoint version means new/updated people are in
    // the DB — refresh the list (never the open image viewers).
    if (status.groups_version !== undefined && status.groups_version !== lastGroupsVersion) {
        lastGroupsVersion = status.groups_version;
        refreshPeopleList();
    }

    if (!status.running) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        $("#run-btn").disabled = false;
        $("#cancel-btn").classList.add("hidden");
        document.body.classList.remove("is-running");
        refresh();
    }
}
