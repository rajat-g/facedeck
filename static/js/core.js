/* Shared state, DOM helpers, API client, settings persistence */

export const PAGE_SIZE = 12;
export const SETTINGS_KEY = "facedeck-settings";

export const state = {
    groups: [],
    moveContext: null,
    pollTimer: null,
    query: "",
    minFaces: 0,
    sortBy: "default",
    page: 1,
    selection: new Set(), // "groupId/filename"
    lastSelectedKey: null,
    lightbox: null,
};

export const $ = (sel) => document.querySelector(sel);

export function getDbFile() {
    return $("#db-file").value.trim() || "processing_state.db";
}

export async function api(path, options = {}) {
    const url = new URL(path, window.location.origin);
    if (!["POST", "DELETE"].includes(options.method || "GET") && !path.includes("db_file")) {
        url.searchParams.set("db_file", getDbFile());
    }
    const res = await fetch(url, options);
    let body = {};
    try {
        body = await res.json();
    } catch (_) {
        /* empty body */
    }
    if (!res.ok) {
        throw new Error(body.error || `Request failed (${res.status})`);
    }
    return body;
}

export function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

export function dirname(path) {
    if (!path) return undefined;
    const idx = Math.max(path.lastIndexOf("\\"), path.lastIndexOf("/"));
    return idx > 0 ? path.slice(0, idx) : undefined;
}

export function faceUrl(group, filename) {
    return `/api/groups/${group.id}/faces/${encodeURIComponent(filename)}?db_file=${encodeURIComponent(getDbFile())}`;
}

export function sourceUrl(groupId, path) {
    return `/api/source-image?group_id=${groupId}&path=${encodeURIComponent(path)}&db_file=${encodeURIComponent(getDbFile())}`;
}

/* ---------- Settings persistence ---------- */

export function loadSettings() {
    let saved = {};
    try {
        saved = JSON.parse(localStorage.getItem(SETTINGS_KEY)) || {};
    } catch (_) {
        /* corrupted settings ignored */
    }
    if (saved.inputFolders) $("#input-folders").value = saved.inputFolders;
    if (saved.outputFaces) $("#output-faces").value = saved.outputFaces;
    if (saved.dbFile) $("#db-file").value = saved.dbFile;
    if (saved.outputFile) $("#output-file").value = saved.outputFile;
    if (saved.threshold) {
        $("#threshold").value = saved.threshold;
        $("#threshold-value").textContent = parseFloat(saved.threshold).toFixed(2);
    }
    const theme = saved.theme || "dark";
    document.documentElement.dataset.theme = theme;
    $("#theme-btn").innerHTML = theme === "dark" ? "&#9788;" : "&#9789;";
}

export function saveSettings() {
    localStorage.setItem(
        SETTINGS_KEY,
        JSON.stringify({
            inputFolders: $("#input-folders").value,
            outputFaces: $("#output-faces").value,
            dbFile: getDbFile(),
            outputFile: $("#output-file").value,
            threshold: $("#threshold").value,
            theme: document.documentElement.dataset.theme || "dark",
        }),
    );
}
