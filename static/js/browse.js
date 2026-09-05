/* Native folder/file picker dialogs via the backend */

import { $, api, dirname, saveSettings } from "./core.js";
import { toastError } from "./toast.js";

async function browsePath(mode, initialdir) {
    try {
        const data = await api("/api/browse", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ mode, initialdir }),
        });
        return data.path;
    } catch (err) { toastError(err.message); return null; }
}

export function initBrowse() {
    $("#browse-folder-btn").addEventListener("click", async () => {
        const ta = $("#input-folders");
        const lines = ta.value.split("\n").filter((l) => l.trim());
        const lastLine = lines[lines.length - 1];
        const path = await browsePath("folder", dirname(lastLine));
        if (path) {
            lines.push(path.replace(/\//g, "\\"));
            ta.value = lines.join("\n");
            saveSettings();
            ta.dispatchEvent(new Event("change"));
        }
    });

    $("#browse-db-btn").addEventListener("click", async () => {
        const input = $("#db-file");
        const path = await browsePath("save", dirname(input.value));
        if (path) {
            input.value = path.replace(/\//g, "\\");
            saveSettings();
        }
    });
}
