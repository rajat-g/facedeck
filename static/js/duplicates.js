/* Duplicates panel: exact sets (delete copies) + near-dupe review
   (preview / link-as-same / dismiss). Nothing here auto-changes data. */

import { $, api, basename, escapeHtml, getDbFile } from "./core.js";
import { toastError, toastSuccess } from "./toast.js";
import { openSourceViewer } from "./lightbox.js";
import { revealInExplorer } from "./ops.js";

let refresh = () => {};
let exactCopies = 0;
let exactSets = 0;
let nearSets = 0;

function renderDupesCount() {
    const parts = [];
    if (exactSets) {
        parts.push(`${exactCopies} cop${exactCopies === 1 ? "y" : "ies"} in ${exactSets} set${exactSets === 1 ? "" : "s"}`);
    }
    if (nearSets) {
        parts.push(`${nearSets} lookalike set${nearSets === 1 ? "" : "s"}`);
    }
    $("#dupes-count").textContent = parts.length ? `— ${parts.join(" · ")}` : "";
}

export function initDuplicates(loadGroups) {
    refresh = loadGroups;
    $("#dupes-refresh").addEventListener("click", loadDuplicates);
    $("#dupes-scan").addEventListener("click", scanNear);
}

export async function loadDuplicates() {
    let data;
    try {
        data = await api("/api/duplicates");
    } catch (err) {
        toastError(`Failed to load duplicates: ${err.message}`);
        return;
    }
    renderExact(data.sets || []);
}

function groupNames(photo) {
    return (photo.groups || []).map((g) => g.name || "?").join(", ") || "no group";
}

function firstGroupId(photo) {
    return photo.groups && photo.groups.length ? photo.groups[0].id : null;
}

/* ---------- Exact sets ---------- */

function renderExact(sets) {
    exactCopies = sets.reduce((s, x) => s + x.duplicates.length, 0);
    exactSets = sets.length;
    renderDupesCount();
    const box = $("#dupes-exact");
    box.innerHTML = "";
    if (!sets.length) {
        box.innerHTML =
            `<p class="muted small dupe-empty">No exact duplicates. Copies are linked automatically on the next run.</p>`;
        return;
    }
    for (const s of sets) box.appendChild(renderExactSet(s));
}

function renderExactSet(s) {
    const div = document.createElement("div");
    div.className = "dupe-set";
    const canonName = basename(s.canonical);
    div.innerHTML =
        `<div class="dupe-head">` +
        `<span class="avatar dupe-avatar">${escapeHtml(canonName.slice(0, 2).toUpperCase())}</span>` +
        `<span class="person-meta"><span class="person-name">${escapeHtml(canonName)}</span>` +
        `<span class="person-sub">${escapeHtml(s.groups.join(", ") || "no group")} · ` +
        `${s.duplicates.length} cop${s.duplicates.length === 1 ? "y" : "ies"}` +
        `${s.canonical_exists ? "" : " · original file missing"}</span></span>` +
        `<span class="person-side"><button class="btn danger delete-all">Delete copies</button></span>` +
        `</div><div class="photo-rows"></div>`;
    const rows = div.querySelector(".photo-rows");
    for (const d of s.duplicates) {
        rows.appendChild(exactRow(s, d));
    }
    div.querySelector(".delete-all").addEventListener("click", async () => {
        const paths = s.duplicates.map((d) => d.path);
        if (!confirm(`Send ${paths.length} cop${paths.length === 1 ? "y" : "ies"} of “${canonName}” to the Recycle Bin?\n\nThe original is kept. Restore from the Recycle Bin if needed — there is no in-app undo.`)) return;
        await deleteDupes(paths);
    });
    return div;
}

function previewGroup(photo) {
    // Minimal group for the guarded single-image viewer.
    const gid = firstGroupId(photo);
    if (gid == null) return null;
    const gname = (photo.groups[0] && photo.groups[0].name) || "Review";
    return { id: gid, name: gname };
}

function exactRow(s, d) {
    const row = document.createElement("div");
    row.className = "photo-row";
    row.title = d.path;
    row.innerHTML =
        `<span class="photo-ico">⧉</span>` +
        `<span class="photo-meta"><span class="photo-name">${escapeHtml(basename(d.path))}</span>` +
        `<span class="photo-path">${escapeHtml(d.path)}${d.exists ? "" : " (file missing)"}</span></span>` +
        `<span class="photo-actions"><button class="btn preview">Preview</button>` +
        `<button class="btn reveal">Reveal</button>` +
        `<button class="btn danger delete-one">Delete</button></span>`;
    row.querySelector(".preview").addEventListener("click", () => {
        const gid = s.group_ids[0];
        if (gid == null) { toastError("No group to preview from."); return; }
        openSourceViewer({ id: gid, name: s.groups[0] || "Review" }, [d.path], 0, 1);
    });
    row.querySelector(".reveal").addEventListener("click", async (e) => {
        e.stopPropagation();
        const gid = s.group_ids[0];
        if (gid == null) { toastError("No group to reveal from."); return; }
        const res = await revealInExplorer(gid, d.path);
        if (res) toastSuccess("Opened in Explorer.");
    });
    row.querySelector(".delete-one").addEventListener("click", async () => {
        if (!confirm(`Send this copy to the Recycle Bin?\n\n${d.path}\n\nThe original is kept.`)) return;
        await deleteDupes([d.path]);
    });
    return row;
}

async function deleteDupes(paths) {
    let result;
    try {
        result = await api("/api/duplicates", {
            method: "DELETE",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ db_file: getDbFile(), paths }),
        });
    } catch (err) {
        toastError(err.message);
        return;
    }
    if (result.errors && result.errors.length) toastError(result.errors.join("; "));
    if (result.deleted && result.deleted.length) {
        toastSuccess(`${result.deleted.length} cop${result.deleted.length === 1 ? "y" : "ies"} moved to the Recycle Bin.`);
    }
    loadDuplicates();
    refresh();
    if (document.querySelector("#dupes-near .dupe-set")) scanNear();
}

/* ---------- Near-dupe review ("possibly the same") ---------- */

async function scanNear() {
    const btn = $("#dupes-scan");
    const threshold = parseInt($("#dupes-threshold").value, 10) || 10;
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = "Scanning…";
    try {
        const data = await api("/api/duplicates/scan", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                db_file: getDbFile(),
                threshold,
            }),
        });
        renderNear(data.sets || [], data.scanned || 0, data.threshold);
    } catch (err) {
        toastError(err.message);
    } finally {
        btn.disabled = false;
        btn.textContent = old;
    }
}

function renderNear(sets, scanned, threshold) {
    nearSets = sets.length;
    renderDupesCount();
    const box = $("#dupes-near");
    box.innerHTML = "";
    const head = document.createElement("p");
    head.className = "muted small";
    head.textContent = sets.length
        ? `${sets.length} lookalike set${sets.length === 1 ? "" : "s"} at threshold ${threshold} (${scanned} photos scanned). Nothing is linked or skipped — your call per photo.`
        : `No lookalikes at threshold ${threshold} (${scanned} photos scanned).`;
    box.appendChild(head);
    for (const s of sets) box.appendChild(renderNearSet(s));
}

function renderNearSet(s) {
    const div = document.createElement("div");
    div.className = "dupe-set near";
    const head = document.createElement("div");
    head.className = "dupe-head";
    head.innerHTML =
        `<span class="avatar dupe-avatar">?</span>` +
        `<span class="person-meta"><span class="person-name">Possibly the same</span>` +
        `<span class="person-sub">${s.photos.length} photos` +
        `${s.pairs.length ? ` · smallest distance ${Math.min(...s.pairs.map((p) => p[2]))}` : ""}</span></span>`;
    div.appendChild(head);
    const rows = document.createElement("div");
    rows.className = "photo-rows";
    for (const p of s.photos) rows.appendChild(nearRow(s, p));
    div.appendChild(rows);
    return div;
}

function nearRow(s, p) {
    const others = s.photos.filter((x) => x.path !== p.path);
    const row = document.createElement("div");
    row.className = "photo-row";
    row.title = p.path;
    row.innerHTML =
        `<span class="photo-ico">?</span>` +
        `<span class="photo-meta"><span class="photo-name">${escapeHtml(basename(p.path))}</span>` +
        `<span class="photo-path">${escapeHtml(p.path)}</span>` +
        `<span class="photo-sub muted small">${escapeHtml(groupNames(p))}</span></span>` +
        `<span class="photo-actions"><button class="btn preview">Preview</button>` +
        `<label class="mini-link">Same as <select class="link-target">` +
        others.map((o) => `<option value="${escapeHtml(o.path)}">${escapeHtml(basename(o.path))}</option>`).join("") +
        `</select></label>` +
        `<button class="btn primary link">Link</button>` +
        `<button class="btn ghost dismiss">Keep both</button></span>`;
    const previewBtn = row.querySelector(".preview");
    previewBtn.disabled = firstGroupId(p) == null;
    previewBtn.title = firstGroupId(p) == null ? "No group to preview from" : "Preview";
    previewBtn.addEventListener("click", () => {
        const g = previewGroup(p);
        if (!g) return;
        openSourceViewer(g, [p.path], 0, 1);
    });
    row.querySelector(".link").addEventListener("click", async () => {
        const target = row.querySelector(".link-target").value;
        if (!target) return;
        if (!confirm(`Treat as the same photo?\n\n${p.path}\n→ alias of\n${target}\n\nIt will appear in the same groups; its own links are dropped.`)) return;
        try {
            await api("/api/duplicates/link", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    db_file: getDbFile(),
                    dup: p.path, canonical: target,
                }),
            });
        } catch (err) {
            toastError(err.message);
            return;
        }
        toastSuccess("Linked — photo now follows its canonical.");
        loadDuplicates();
        refresh();
        scanNear();
    });
    row.querySelector(".dismiss").addEventListener("click", async () => {
        const pairs = others.map((o) => [p.path, o.path]);
        try {
            await api("/api/duplicates/dismiss", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    db_file: getDbFile(), pairs,
                }),
            });
        } catch (err) {
            toastError(err.message);
            return;
        }
        scanNear();
    });
    return row;
}
