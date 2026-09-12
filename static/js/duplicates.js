/* Duplicates panel: exact sets (delete copies) + near-dupe review
   (preview / link-as-same / dismiss). Nothing here auto-changes data. */

import { $, api, basename, escapeHtml, getDbFile, shortDir } from "./core.js";
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
    document.dispatchEvent(new CustomEvent("counts-changed"));
}

export function getDupeSetCount() {
    return (exactSets || 0) + (nearSets || 0);
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
    row.className = "photo-row lines";
    row.title = d.path;
    row.innerHTML =
        `<span class="photo-ico">⧉</span>` +
        `<span class="photo-meta"><span class="photo-name">${escapeHtml(basename(d.path))}</span>` +
        `<span class="photo-sub muted small">${escapeHtml(shortDir(d.path))}${d.exists ? "" : " · file missing"}</span></span>` +
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

function pairPct(pr) {
    // Pairs are [a, b, dist, match_pct]; recompute when pct is missing.
    if (pr.length > 3 && Number.isFinite(Number(pr[3]))) return Number(pr[3]);
    const d = Number(pr[2]);
    if (!Number.isFinite(d)) return null;
    return Math.round(((64 - d) * 100) / 64 * 10) / 10;
}

function bestPairPct(s) {
    let best = null;
    for (const pr of s.pairs || []) {
        const pct = pairPct(pr);
        if (pct != null && (best == null || pct > best)) best = pct;
    }
    return best;
}

function closestPct(s, path) {
    let best = null;
    let other = null;
    for (const pr of s.pairs || []) {
        if (pr[0] !== path && pr[1] !== path) continue;
        const pct = pairPct(pr);
        if (pct != null && (best == null || pct > best)) {
            best = pct;
            other = pr[0] === path ? pr[1] : pr[0];
        }
    }
    return { pct: best, other };
}

function renderNearSet(s) {
    const div = document.createElement("div");
    div.className = "dupe-set near";
    const best = s.best_pct != null ? Number(s.best_pct) : bestPairPct(s);
    const head = document.createElement("div");
    head.className = "dupe-head";
    head.innerHTML =
        `<span class="person-meta"><span class="person-name">Possibly the same</span>` +
        `<span class="person-sub">${s.photos.length} photos` +
        (best != null ? ` · best match ${best}%` : "") + `</span></span>` +
        (best != null
            ? `<span class="match-pill" title="Closest perceptual-hash match in this set">~${best}%</span>`
            : "");
    div.appendChild(head);

    // One row per photo (no mirrored pairs): tick the ones to link, pick the
    // canonical once below. Scales to burst sets — N compact rows + 2 buttons.
    const state = {
        canonical: s.photos[0].path,
        checked: new Set(s.photos.slice(1).map((p) => p.path)),
    };
    const checks = new Map();
    const rows = document.createElement("div");
    rows.className = "photo-rows";
    for (const p of s.photos) rows.appendChild(nearRow(s, p, state, checks));
    div.appendChild(rows);

    const syncChecks = () => {
        for (const [path, box] of checks) {
            box.checked = state.checked.has(path);
            box.disabled = path === state.canonical;
            box.title = path === state.canonical
                ? "Canonical — the photo the checked ones will follow"
                : "Link this photo as the same";
        }
    };

    const foot = document.createElement("div");
    foot.className = "dupe-foot";
    const canonSel = document.createElement("select");
    canonSel.className = "canon-select";
    canonSel.title = "The photo the checked ones will follow";
    for (const p of s.photos) {
        const o = document.createElement("option");
        o.value = p.path;
        o.textContent = basename(p.path);
        canonSel.appendChild(o);
    }
    const linkBtn = document.createElement("button");
    linkBtn.className = "btn primary link-all";
    linkBtn.textContent = "Link checked";
    const keepBtn = document.createElement("button");
    keepBtn.className = "btn ghost keep-all";
    keepBtn.textContent = "Keep all separate";
    foot.append(
        Object.assign(document.createElement("span"), {
            className: "muted small", textContent: "Same as",
        }),
        canonSel, linkBtn, keepBtn,
    );
    div.appendChild(foot);

    canonSel.addEventListener("change", () => {
        state.canonical = canonSel.value;
        state.checked.delete(state.canonical);
        syncChecks();
    });
    rows.addEventListener("change", (e) => {
        const box = e.target.closest(".link-check");
        if (!box) return;
        if (box.checked) state.checked.add(box.dataset.path);
        else state.checked.delete(box.dataset.path);
    });
    linkBtn.addEventListener("click", async () => {
        const targets = s.photos
            .map((p) => p.path)
            .filter((p) => p !== state.canonical && state.checked.has(p));
        if (!targets.length) {
            toastError("Nothing checked — tick the photos to link.");
            return;
        }
        if (!confirm(
            `Treat ${targets.length} photo${targets.length === 1 ? "" : "s"} as the same as “${basename(state.canonical)}”?\n\n` +
            `They will appear in the same groups; their own links are dropped.`
        )) return;
        let ok = 0;
        const errs = [];
        for (const t of targets) {
            try {
                await api("/api/duplicates/link", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        db_file: getDbFile(),
                        dup: t, canonical: state.canonical,
                    }),
                });
                ok++;
            } catch (err) {
                errs.push(`${basename(t)}: ${err.message}`);
            }
        }
        if (ok) toastSuccess(`Linked ${ok} photo${ok === 1 ? "" : "s"} — now following “${basename(state.canonical)}”.`);
        if (errs.length) toastError(errs.join("; "));
        loadDuplicates();
        refresh();
        scanNear();
    });
    keepBtn.addEventListener("click", async () => {
        const pairs = (s.pairs || []).map((pr) => [pr[0], pr[1]]);
        if (!pairs.length) return;
        try {
            await api("/api/duplicates/dismiss", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ db_file: getDbFile(), pairs }),
            });
        } catch (err) {
            toastError(err.message);
            return;
        }
        toastSuccess("Kept separate — this set won't be suggested again.");
        scanNear();
    });
    syncChecks();
    return div;
}

function nearRow(s, p, state, checks) {
    const { pct, other } = closestPct(s, p.path);
    const isCanon = p.path === state.canonical;
    const sub = [shortDir(p.path), groupNames(p)]
        .filter(Boolean)
        .join(" · ") + (pct != null
            ? ` · closest ${pct}%${other ? ` vs ${basename(other)}` : ""}`
            : "");
    const row = document.createElement("div");
    row.className = "photo-row lines";
    row.title = p.path;
    row.innerHTML =
        `<input type="checkbox" class="link-check" data-path="${escapeHtml(p.path)}">` +
        `<span class="photo-meta"><span class="photo-name">${escapeHtml(basename(p.path))}</span>` +
        `<span class="photo-sub muted small">${escapeHtml(sub)}</span></span>` +
        `<span class="photo-actions"><button class="btn preview">Preview</button></span>`;
    const box = row.querySelector(".link-check");
    box.checked = !isCanon;
    box.disabled = isCanon;
    checks.set(p.path, box);
    const previewBtn = row.querySelector(".preview");
    previewBtn.disabled = firstGroupId(p) == null;
    previewBtn.title = firstGroupId(p) == null ? "No group to preview from" : "Preview";
    previewBtn.addEventListener("click", () => {
        const g = previewGroup(p);
        if (!g) return;
        openSourceViewer(g, [p.path], 0, 1);
    });
    return row;
}
