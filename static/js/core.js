/* Shared state, helpers, API client, settings */

export const PEOPLE_PER_PAGE = 20;
export const FACES_PER_PAGE = 48;
export const PHOTOS_PER_PAGE_LIST = 50;
export const PHOTOS_PER_PAGE_THUMBS = 30;
export const SETTINGS_KEY = "facedeck-settings-v2";

export const state = {
    groups: [], // summary: {id,name,directory,face_count,photo_count,mtime}
    selectedId: null,
    query: "",
    minFaces: 0,
    sortBy: "default",
    statusFilter: "all", // all | pending | approved
    page: 1,
    selection: new Set(), // "groupId/filename" for face crops
    lastSelectedKey: null,
    // per-person viewers (opt-in, paginated)
    faces: { items: [], total: 0, page: 1, visible: false, loading: false },
    photos: { items: [], total: 0, page: 1, mode: "list", query: "", visible: true, loading: false },
    lightbox: null, // {kind:'face'|'source', groupId, groupName, items, index}
    moveContext: null,
    pollTimer: null,
};

export const $ = (sel) => document.querySelector(sel);

export function isRunning() {
    return document.body.classList.contains("is-running");
}

export function getDbFile() {
    return $("#db-file").value.trim() || "processing_state.db";
}

export async function api(path, options = {}) {
    const url = new URL(path, window.location.origin);
    const method = (options.method || "GET").toUpperCase();
    if (!["POST", "DELETE"].includes(method) && !path.includes("db_file")) {
        url.searchParams.set("db_file", getDbFile());
    }
    const res = await fetch(url, options);
    let body = {};
    try {
        body = await res.json();
    } catch (_) { /* empty */ }
    if (!res.ok) throw new Error(body.error || `Request failed (${res.status})`);
    return body;
}

export function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    // innerHTML escapes &<> but NOT quotes — escape those too since values
    // land in double-quoted attributes (titles, alt text).
    return div.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

export function dirname(path) {
    if (!path) return undefined;
    const idx = Math.max(path.lastIndexOf("\\"), path.lastIndexOf("/"));
    return idx > 0 ? path.slice(0, idx) : undefined;
}

export function basename(path) {
    if (!path) return "";
    return String(path).split(/[/\\]/).pop();
}

export function faceUrl(groupId, filename) {
    return `/api/groups/${groupId}/faces/${encodeURIComponent(filename)}?db_file=${encodeURIComponent(getDbFile())}`;
}

export function sourceUrl(groupId, path) {
    return `/api/source-image?group_id=${groupId}&path=${encodeURIComponent(path)}&db_file=${encodeURIComponent(getDbFile())}`;
}

export function initials(name) {
    const clean = (name || "?").trim();
    if (!clean) return "?";
    const parts = clean.split(/[\s_\-]+/).filter(Boolean);
    if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

const AVATAR_PAIRS = [
    ["#5b7cfa", "#8a5cf6"], ["#0ea5a5", "#2563eb"], ["#f59e0b", "#ef4444"],
    ["#22c55e", "#0ea5e9"], ["#ec4899", "#8b5cf6"], ["#f97316", "#eab308"],
    ["#14b8a6", "#22d3ee"], ["#6366f1", "#d946ef"],
];

export function avatarColors(name) {
    let h = 0;
    for (const ch of String(name || "?")) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return AVATAR_PAIRS[h % AVATAR_PAIRS.length];
}

/* ---------- Settings ---------- */

export function loadSettings() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(SETTINGS_KEY)) || {}; } catch (_) {}
    // fall back to v1 key once
    if (Object.keys(saved).length === 0) {
        try { saved = JSON.parse(localStorage.getItem("facedeck-settings")) || {}; } catch (_) {}
    }
    if (saved.inputFolders) $("#input-folders").value = saved.inputFolders;
    if (saved.outputFaces) $("#output-faces").value = saved.outputFaces;
    if (saved.dbFile) $("#db-file").value = saved.dbFile;
    if (saved.threshold) {
        $("#threshold").value = saved.threshold;
        $("#threshold-value").textContent = parseFloat(saved.threshold).toFixed(2);
    }
    const theme = saved.theme || "dark";
    document.documentElement.dataset.theme = theme;
    $("#theme-btn").textContent = theme === "dark" ? "☾" : "☀";
    // Collapse only when the user explicitly collapsed before; first-timers
    // get an open Setup panel instead of an empty list with a hidden door.
    const collapsed = saved.configCollapsed === true;
    $("#config-panel").classList.toggle("collapsed", collapsed);
    $("#config-toggle").setAttribute("aria-expanded", collapsed ? "false" : "true");
    if (saved.photosMode === "thumbs") state.photos.mode = "thumbs";
}

export function saveSettings() {
    try {
        localStorage.setItem(SETTINGS_KEY, JSON.stringify({
            inputFolders: $("#input-folders").value,
            outputFaces: $("#output-faces").value,
            dbFile: getDbFile(),
            threshold: $("#threshold").value,
            theme: document.documentElement.dataset.theme || "dark",
            configCollapsed: $("#config-panel").classList.contains("collapsed"),
            photosMode: state.photos.mode,
        }));
    } catch (_) {}
}
