/* People list (master) + person detail with opt-in, paginated previews.
   No images load until the user explicitly asks — safe for thousands of photos. */

import {
    $,
    PEOPLE_PER_PAGE,
    FACES_PER_PAGE,
    PHOTOS_PER_PAGE_LIST,
    PHOTOS_PER_PAGE_THUMBS,
    api,
    avatarColors,
    basename,
    escapeHtml,
    faceUrl,
    getDbFile,
    initials,
    saveSettings,
    sourceUrl,
    state,
} from "./core.js";
import { photosSuffix, toastError, toastSuccess } from "./toast.js";
import {
    approveAllGroups,
    approveGroup,
    deleteFaces,
    deleteGroup,
    moveFaces,
    renameGroup,
    revealGroupFolder,
    revealInExplorer,
} from "./ops.js";
import { openFaceViewer, openSourceViewer } from "./lightbox.js";

let externalRefresh = null;
let facesReq = 0;
let photosReq = 0;
let photosSearchTimer = null;

export function initGroups(loadGroupsFn) {
    externalRefresh = loadGroupsFn || null;

    const searchInput = $("#filter-text");
    const clearBtn = $("#filter-clear");
    const syncClear = () => clearBtn.classList.toggle("hidden", !searchInput.value.trim());
    searchInput.addEventListener("input", (e) => {
        state.query = e.target.value.trim();
        state.page = 1;
        syncClear();
        renderPeople();
    });
    clearBtn.addEventListener("click", () => {
        searchInput.value = "";
        state.query = "";
        state.page = 1;
        syncClear();
        renderPeople();
        searchInput.focus();
    });
    $("#filter-min-faces").addEventListener("input", (e) => {
        state.minFaces = parseInt(e.target.value, 10) || 0;
        state.page = 1;
        renderPeople();
    });
    $("#sort-by").addEventListener("change", (e) => {
        state.sortBy = e.target.value;
        renderPeople();
    });
    document.querySelectorAll(".pills .pill").forEach((btn) => {
        btn.addEventListener("click", () => {
            document.querySelectorAll(".pills .pill").forEach((b) => b.classList.remove("active"));
            btn.classList.add("active");
            state.statusFilter = btn.dataset.status;
            state.page = 1;
            renderPeople();
        });
    });
    $("#page-prev").addEventListener("click", () => { state.page -= 1; renderPeople(); });
    $("#page-next").addEventListener("click", () => { state.page += 1; renderPeople(); });

    $("#bulk-move-btn").addEventListener("click", bulkMove);
    $("#bulk-delete-btn").addEventListener("click", bulkDelete);
    $("#bulk-clear-btn").addEventListener("click", clearSelection);
    $("#approve-all-btn").addEventListener("click", approveAll);
}

/* ---------- Data ---------- */

export async function loadGroups() {
    let data;
    try {
        data = await api("/api/groups?summary=1");
    } catch (err) {
        toastError(`Failed to load groups: ${err.message}`);
        return;
    }
    state.groups = data.groups || [];
    pruneSelection();

    if (state.selectedId != null && !state.groups.some((g) => g.id === state.selectedId)) {
        state.selectedId = null;
        resetViewers();
    }
    // Auto-select first person on first load for a friendlier start.
    if (state.selectedId == null) {
        const first = filteredGroups()[0];
        if (first) {
            state.selectedId = first.id;
            resetViewers();
        }
    }
    renderPeople();
    renderDetail();
    // Reload open viewers so counts stay fresh after mutations.
    if (state.selectedId != null) {
        if (state.faces.visible) loadFaces(state.faces.page);
        if (state.photos.visible) loadPhotos(state.photos.page);
    }
    loadStats();
    updateBulkBar();
}

/* Lightweight refresh used while a run streams checkpoints: updates the
   people list, stats and detail counts WITHOUT touching open image
   viewers, selection focus or the name input. */
export async function refreshPeopleList() {
    let data;
    try {
        data = await api("/api/groups?summary=1");
    } catch (_) {
        return;
    }
    state.groups = data.groups || [];
    pruneSelection();

    if (state.selectedId == null) {
        const first = filteredGroups()[0];
        if (first) {
            state.selectedId = first.id;
            resetViewers();
            renderDetail();
        }
    } else if (!state.groups.some((g) => g.id === state.selectedId)) {
        state.selectedId = null;
        resetViewers();
        renderDetail();
    } else {
        updateDetailCounts();
    }
    renderPeople();
    updateBulkBar();
    loadStats();
}

/* Patch counts/badges in the open detail pane in place (never rebuilds
   inputs or viewers, so focus and loaded images survive streaming). */
function updateDetailCounts() {
    const g = selectedGroup();
    const pane = $("#person-detail");
    if (!g || !pane || !pane.querySelector(".detail-name")) return;
    const badges = pane.querySelector(".badges");
    if (badges) {
        badges.innerHTML = badgesHtml(g);
    }
    const facesCount = pane.querySelector("#faces-section .count");
    if (facesCount) facesCount.textContent = g.face_count;
    const photosCount = pane.querySelector("#photos-section .count");
    if (photosCount) photosCount.textContent = g.photo_count;
    const toggle = pane.querySelector(".faces-toggle");
    if (toggle && !state.faces.visible) {
        toggle.textContent = g.face_count ? "View on UI" : "View";
    }
    const approveBtn = pane.querySelector(".approve-one");
    if (approveBtn) approveBtn.disabled = g.face_count === 0;
}

async function loadStats() {
    let stats;
    try { stats = await api("/api/stats"); } catch (_) { return; }
    const panel = $("#stats-panel");
    panel.classList.toggle("hidden", stats.groups === 0);
    if (stats.groups === 0) return;
    const top = (stats.largest || []).slice(0, 3)
        .map((g) => `${escapeHtml(g.name)} (${g.count})`).join(", ");
    $("#stats-summary").innerHTML =
        `<span class="chip">${stats.photos} photos</span>` +
        `<span class="chip">${stats.faces} faces</span>` +
        `<span class="chip">${stats.groups} people</span>` +
        (top ? `<span class="chip dim" title="Largest groups">Top: ${top}</span>` : "");
    const hs = $("#header-stat");
    hs.classList.remove("hidden");
    $("#header-stat-text").textContent = `${stats.groups} people · ${stats.photos} photos`;
}

function resetViewers() {
    state.faces = { items: [], total: 0, page: 1, visible: false, loading: false };
    // Photo list (text rows, no images) opens by default; thumbnail mode
    // stays opt-in since it loads real images per selection.
    const listByDefault = (state.photos.mode || "list") === "list";
    state.photos = { items: [], total: 0, page: 1, mode: state.photos.mode || "list", query: "", visible: listByDefault, loading: false };
    state.selection.clear();
}

/* ---------- Filter / sort / paginate people ---------- */

function isApproved(g) { return g.face_count === 0 && g.photo_count > 0; }

function isEmptyGroup(g) { return g.face_count === 0 && g.photo_count === 0; }

function badgesHtml(g) {
    const approved = isApproved(g);
    return `<span class="badge">${g.face_count} faces</span>` +
        `<span class="badge">${g.photo_count} photos</span>` +
        (isEmptyGroup(g)
            ? `<span class="badge">Empty</span>`
            : `<span class="badge ${approved ? "approved" : "pending"}">` +
              `${approved ? "✓ Approved" : "Pending"}</span>`);
}

function filteredGroups() {
    const q = state.query.toLowerCase();
    const list = state.groups.filter((g) => {
        if (g.photo_count < state.minFaces) return false;
        if (state.statusFilter === "pending" && isApproved(g)) return false;
        if (state.statusFilter === "approved" && !isApproved(g)) return false;
        if (q && !g.name.toLowerCase().includes(q)) return false;
        return true;
    });
    const byName = (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
    switch (state.sortBy) {
        case "name": list.sort(byName); break;
        case "size-desc": list.sort((a, b) => b.photo_count - a.photo_count || byName(a, b)); break;
        case "size-asc": list.sort((a, b) => a.photo_count - b.photo_count || byName(a, b)); break;
        case "newest": list.sort((a, b) => (b.mtime || 0) - (a.mtime || 0)); break;
        default: list.sort((a, b) => a.id - b.id);
    }
    return list;
}

function selectedGroup() {
    return state.groups.find((g) => g.id === state.selectedId) || null;
}

export function selectPerson(groupId) {
    if (state.selectedId === groupId) return;
    if (!state.groups.some((g) => g.id === groupId)) return;
    state.selectedId = groupId;
    resetViewers();
    state.selection.clear();
    renderPeople();
    renderDetail();
    updateBulkBar();
    // Photo list opens by default (text rows, no images). Face crops and
    // thumbnail mode stay click-to-view. Exactly one fetch per selection.
    if (state.photos.visible) loadPhotos(1);
    document.querySelector("#person-detail")?.scrollIntoView({ block: "nearest" });
}

/* ---------- People list ---------- */

function renderPeople() {
    const listEl = $("#people-list");
    listEl.innerHTML = "";
    const filtered = filteredGroups();
    const pages = Math.max(1, Math.ceil(filtered.length / PEOPLE_PER_PAGE));
    state.page = Math.min(Math.max(1, state.page), pages);

    const total = state.groups.length;
    const pending = state.groups.filter((g) => !isApproved(g)).length;
    const approved = total - pending;
    $("#groups-count").textContent = total ? `— ${total} people · ${pending} pending · ${approved} approved` : "";
    $("#people-count").textContent = filtered.length ? `${filtered.length} shown` : "";

    const summary = $("#filter-summary");
    if (state.query || state.statusFilter !== "all" || state.minFaces > 0) {
        summary.textContent = `Showing ${filtered.length} of ${total} · “${state.query || "—"}” · ${state.statusFilter} · min ${state.minFaces} photos`;
        summary.classList.remove("hidden");
    } else { summary.textContent = ""; summary.classList.add("hidden"); }

    const empty = $("#groups-empty");
    const isEmpty = filtered.length === 0;
    empty.classList.toggle("hidden", !isEmpty);
    if (isEmpty) {
        empty.innerHTML = state.groups.length === 0
            ? `<div class="empty-ico">⊘</div><h3>No groups yet</h3>` +
              `<p class="muted">Run grouping or click Refresh to load existing results.</p>` +
              `<p class="muted small mono" title="Active database file">DB: ${escapeHtml(getDbFile())}</p>`
            : `<div class="empty-ico">⊘</div><h3>No matches</h3><p class="muted">Try clearing the search or status filter.</p>`;
    }

    $("#pagination").classList.toggle("hidden", pages <= 1);
    $("#page-info").textContent = `Page ${state.page} of ${pages}`;
    $("#page-prev").disabled = state.page <= 1;
    $("#page-next").disabled = state.page >= pages;

    const start = (state.page - 1) * PEOPLE_PER_PAGE;
    for (const g of filtered.slice(start, start + PEOPLE_PER_PAGE)) {
        listEl.appendChild(renderPersonRow(g));
    }
}

function renderPersonRow(g) {
    const approved = isApproved(g);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "person-row" + (g.id === state.selectedId ? " selected" : "");
    btn.setAttribute("role", "option");
    btn.setAttribute("aria-selected", g.id === state.selectedId ? "true" : "false");
    const [c1, c2] = avatarColors(g.name);
    btn.innerHTML =
        `<span class="avatar" style="--av1:${c1};--av2:${c2}">${escapeHtml(initials(g.name))}</span>` +
        `<span class="person-meta"><span class="person-name">${escapeHtml(g.name)}</span>` +
        `<span class="person-sub">${g.face_count} faces · ${g.photo_count} photos</span></span>` +
        `<span class="person-side"><span class="status-dot ${approved ? "approved" : "pending"}" title="${approved ? "Approved" : "Pending"}"></span>` +
        `<span class="chev">›</span></span>`;
    btn.addEventListener("click", () => {
        selectPerson(g.id);
    });
    btn.addEventListener("dragover", (e) => { e.preventDefault(); btn.classList.add("drop-target"); });
    btn.addEventListener("dragleave", () => btn.classList.remove("drop-target"));
    btn.addEventListener("drop", (e) => {
        e.preventDefault();
        btn.classList.remove("drop-target");
        handleDrop(e, g);
    });
    return btn;
}

/* ---------- Detail ---------- */

function renderDetail() {
    const pane = $("#person-detail");
    const g = selectedGroup();
    if (!g) {
        pane.innerHTML =
            `<div class="empty detail-empty"><div class="empty-ico">☺</div><h3>Select a person</h3>` +
            `<p class="muted">Choose someone on the left to review them.<br>Images stay unloaded until you ask for them — fast even with thousands of photos.</p></div>`;
        return;
    }
    const approved = isApproved(g);
    const [c1, c2] = avatarColors(g.name);
    pane.innerHTML =
        `<div class="detail-head">
            <span class="avatar detail-avatar" style="--av1:${c1};--av2:${c2}">${escapeHtml(initials(g.name))}</span>
            <div class="detail-id">
                <input type="text" class="detail-name" value="" title="Person name (stored in database)" aria-label="Person name">
                <div class="badges">
                    ${badgesHtml(g)}
                </div>
                <div class="folder-path" title="${escapeHtml(g.directory)}">${escapeHtml(g.directory)}</div>
            </div>
        </div>
        <div class="detail-actions">
            <button class="btn rename-save" title="Save person name">✎ Save name</button>
            <button class="btn copy-paths" title="Copy all source photo paths">⧉ Copy paths</button>
            <button class="btn open-folder" title="Open this person's folder in Explorer">🗁 Open folder</button>
            <button class="btn ${approved ? "" : "success"} approve-one" ${g.face_count === 0 ? "disabled" : ""} title="Approve — permanently delete cropped faces">✓ Approve</button>
            ${isEmptyGroup(g) ? '<button class="btn danger delete-group" title="Delete this empty group (nothing to lose — it has no faces or photos)">🗑 Delete group</button>' : ""}
            <span class="run-lock-note" title="Move, delete, approve and undo are paused while a grouping run is in progress">⏸ Curation paused during run</span>
        </div>
        <div class="section" id="faces-section">
            <div class="section-head">
                <h3>Face crops</h3><span class="count">${g.face_count}</span><span class="spacer"></span>
                <button class="btn faces-toggle">${state.faces.visible ? "Hide" : g.face_count ? "View on UI" : "View"}</button>
            </div>
            <div class="section-body" id="faces-body"></div>
        </div>
        <div class="section" id="photos-section">
            <div class="section-head">
                <h3>Source photos</h3><span class="count">${g.photo_count}</span><span class="spacer"></span>
                <div class="seg" role="group" aria-label="Photos view mode">
                    <button class="seg-btn ${state.photos.mode === "list" ? "active" : ""}" data-mode="list">List</button>
                    <button class="seg-btn ${state.photos.mode === "thumbs" ? "active" : ""}" data-mode="thumbs">Thumbs</button>
                </div>
                <span class="mini-search"><input type="search" id="photos-search" placeholder="Filter filenames…" value=""></span>
                <button class="btn photos-toggle">${state.photos.visible ? "Hide" : "View"}</button>
            </div>
            <div class="section-body" id="photos-body"></div>
        </div>`;

    wireDetailHeader(pane, g);
    pane.querySelector(".faces-toggle").addEventListener("click", () => {
        state.faces.visible = !state.faces.visible;
        if (state.faces.visible && state.faces.items.length === 0 && state.faces.total === 0) loadFaces(1);
        else renderDetail();
    });
    pane.querySelectorAll(".seg-btn").forEach((b) => b.addEventListener("click", () => {
        if (state.photos.mode === b.dataset.mode) return;
        state.photos.mode = b.dataset.mode;
        state.photos.page = 1;
        saveSettings();
        pane.querySelectorAll(".seg-btn").forEach((x) => x.classList.toggle("active", x === b));
        if (state.photos.visible) loadPhotos(1);
    }));
    const search = pane.querySelector("#photos-search");
    search.value = state.photos.query || "";
    search.addEventListener("input", () => {
        clearTimeout(photosSearchTimer);
        photosSearchTimer = setTimeout(() => {
            state.photos.query = search.value.trim();
            state.photos.page = 1;
            if (state.photos.visible) loadPhotos(1);
        }, 300);
    });
    pane.querySelector(".photos-toggle").addEventListener("click", () => {
        state.photos.visible = !state.photos.visible;
        if (state.photos.visible) loadPhotos(state.photos.page || 1);
        else renderDetail();
    });

    renderFacesBody();
    renderPhotosBody();
}

function wireDetailHeader(pane, g) {
    const nameInput = pane.querySelector(".detail-name");
    nameInput.value = g.name;
    let saving = false;

    const save = async () => {
        if (saving) return;
        const newName = nameInput.value.trim();
        if (!newName) { toastError("Name cannot be empty."); nameInput.value = g.name; return; }
        if (newName === g.name) return;
        saving = true;
        const saveBtn = pane.querySelector(".rename-save");
        saveBtn.disabled = true;
        const result = await renameGroup(g.id, newName);
        saveBtn.disabled = false;
        saving = false;
        if (!result) { nameInput.value = g.name; nameInput.focus(); return; }
        g.name = newName;
        toastSuccess(`Renamed to “${newName}”.`);
        renderPeople();
        renderDetail();
    };
    pane.querySelector(".rename-save").addEventListener("click", save);
    nameInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); save(); }
        else if (e.key === "Escape") { nameInput.value = g.name; nameInput.blur(); }
    });
    nameInput.addEventListener("blur", () => {
        const v = nameInput.value.trim();
        if (v && v !== g.name) save();
        else if (!v) nameInput.value = g.name;
    });

    pane.querySelector(".copy-paths").addEventListener("click", async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        try {
            const paths = await fetchAllPhotoPaths(g.id);
            await navigator.clipboard.writeText(paths.join("\n"));
            toastSuccess(paths.length ? `${paths.length} path(s) copied.` : "No photos to copy.");
        } catch (err) { toastError(`Could not copy: ${err.message}`); }
        btn.disabled = false;
    });
    pane.querySelector(".open-folder").addEventListener("click", async () => {
        const res = await revealGroupFolder(g.id);
        if (res) toastSuccess("Opened folder in Explorer.");
    });
    const approveBtn = pane.querySelector(".approve-one");
    if (approveBtn) approveBtn.addEventListener("click", async () => {
        if (g.face_count === 0) return;
        if (!confirm(`Approve “${g.name}”?\n\nThis PERMANENTLY deletes ${g.face_count} cropped face file(s).\nSource photos are kept.`)) return;
        approveBtn.disabled = true;
        const result = await approveGroup(g.id);
        if (!result) { approveBtn.disabled = false; return; }
        toastSuccess(`Approved “${g.name}” — ${result.deleted} crop(s) deleted.`);
        loadGroups();
    });
    const deleteBtn = pane.querySelector(".delete-group");
    if (deleteBtn) deleteBtn.addEventListener("click", async () => {
        if (!confirm(`Delete empty group “${g.name}”?\n\nThe group entry will be removed. Nothing else is affected — it has no faces or photos.`)) return;
        deleteBtn.disabled = true;
        const result = await deleteGroup(g.id);
        if (!result) { deleteBtn.disabled = false; return; }
        toastSuccess(`Deleted empty group “${g.name}”. Use Undo to restore.`);
        loadGroups();
    });
}

async function fetchAllPhotoPaths(groupId) {
    const all = [];
    let page = 1;
    for (let i = 0; i < 200; i++) {
        const data = await api(`/api/groups/${groupId}/photos?page=${page}&per_page=200`);
        all.push(...data.photos);
        if (all.length >= data.total || data.photos.length === 0) break;
        page += 1;
    }
    return all;
}

/* ---------- Faces viewer (opt-in) ---------- */

async function loadFaces(page) {
    const g = selectedGroup();
    if (!g) return;
    state.faces.loading = true;
    state.faces.visible = true;
    renderFacesBody();
    const myReq = ++facesReq;
    try {
        const data = await api(`/api/groups/${g.id}/faces?page=${page}&per_page=${FACES_PER_PAGE}`);
        if (myReq !== facesReq || state.selectedId !== g.id) return;
        if (!Array.isArray(data.faces)) throw new Error("bad faces response");
        state.faces.items = data.faces;
        state.faces.total = Number.isFinite(data.total) ? data.total : data.faces.length;
        state.faces.page = Number.isFinite(data.page) ? data.page : page;
    } catch (err) {
        if (myReq !== facesReq) return;
        toastError(`Failed to load faces: ${err.message}`);
    } finally {
        if (myReq === facesReq) { state.faces.loading = false; renderDetailHeads(); renderFacesBody(); }
    }
}

function renderDetailHeads() {
    // Refresh toggle labels + counts without rebuilding inputs (preserves focus).
    const g = selectedGroup();
    if (!g) return;
    const ft = document.querySelector(".faces-toggle");
    if (ft) ft.textContent = state.faces.visible ? "Hide" : "View on UI";
    const pt = document.querySelector(".photos-toggle");
    if (pt) pt.textContent = state.photos.visible ? "Hide" : "View";
}

function renderFacesBody() {
    const body = $("#faces-body");
    if (!body) return;
    const g = selectedGroup();
    if (!g) { body.innerHTML = ""; return; }
    if (!state.faces.visible) {
        body.innerHTML = g.face_count === 0
            ? `<div class="section-note">No cropped faces — nothing to preview.</div>`
            : `<div class="section-note">Previews are off for speed.<br><button class="btn primary faces-show">View ${g.face_count} face(s) on UI</button></div>`;
        const show = body.querySelector(".faces-show");
        if (show) show.addEventListener("click", () => loadFaces(1));
        return;
    }
    if (state.faces.loading && state.faces.items.length === 0) {
        body.innerHTML = `<div class="section-note">Loading faces…</div>`;
        return;
    }
    if (state.faces.total === 0 && state.faces.items.length === 0) {
        body.innerHTML = `<div class="section-note">No cropped faces in this group.</div>`;
        return;
    }
    const pages = Math.max(1, Math.ceil(state.faces.total / FACES_PER_PAGE));
    const grid = document.createElement("div");
    grid.className = "faces-grid";
    for (const filename of state.faces.items) grid.appendChild(renderFaceTile(g, filename));

    body.innerHTML = "";
    const pagerTop = pagerEl(state.faces.page, pages, "faces");
    body.appendChild(pagerTop);
    body.appendChild(grid);
    const hint = document.createElement("div");
    hint.className = "muted small";
    hint.style.marginTop = "10px";
    hint.textContent = "Click to enlarge · Ctrl-click to multi-select · Shift-click for range · drag onto a person to move.";
    body.appendChild(hint);
    wirePager(body, "faces", pages, (p) => loadFaces(p));
}

function pagerEl(page, pages, kind) {
    const wrap = document.createElement("div");
    wrap.className = "mini-pager";
    wrap.innerHTML =
        `<button class="btn pg-prev" ${page <= 1 ? "disabled" : ""}>← Prev</button>` +
        `<span class="muted small mono">Page ${page} of ${pages}</span>` +
        `<button class="btn pg-next" ${page >= pages ? "disabled" : ""}>Next →</button>`;
    wrap.dataset.kind = kind;
    return wrap;
}

function wirePager(body, kind, pages, onPage) {
    const pager = body.querySelector(".mini-pager");
    if (!pager) return;
    pager.querySelector(".pg-prev").addEventListener("click", () => {
        const cur = kind === "faces" ? state.faces.page : state.photos.page;
        if (cur > 1) onPage(cur - 1);
    });
    pager.querySelector(".pg-next").addEventListener("click", () => {
        const cur = kind === "faces" ? state.faces.page : state.photos.page;
        if (cur < pages) onPage(cur + 1);
    });
}

/* ---------- Photos viewer (opt-in; list by default = zero images) ---------- */

async function loadPhotos(page) {
    const g = selectedGroup();
    if (!g) return;
    state.photos.loading = true;
    state.photos.visible = true;
    renderPhotosBody();
    const myReq = ++photosReq;
    const perPage = state.photos.mode === "thumbs" ? PHOTOS_PER_PAGE_THUMBS : PHOTOS_PER_PAGE_LIST;
    try {
        const data = await api(
            `/api/groups/${g.id}/photos?page=${page}&per_page=${perPage}&q=${encodeURIComponent(state.photos.query)}`
        );
        if (myReq !== photosReq || state.selectedId !== g.id) return;
        if (!Array.isArray(data.photos)) throw new Error("bad photos response");
        state.photos.items = data.photos;
        state.photos.total = Number.isFinite(data.total) ? data.total : data.photos.length;
        state.photos.page = Number.isFinite(data.page) ? data.page : page;
    } catch (err) {
        if (myReq !== photosReq) return;
        toastError(`Failed to load photos: ${err.message}`);
    } finally {
        if (myReq === photosReq) { state.photos.loading = false; renderDetailHeads(); renderPhotosBody(); }
    }
}

function renderPhotosBody() {
    const body = $("#photos-body");
    if (!body) return;
    const g = selectedGroup();
    if (!g) { body.innerHTML = ""; return; }
    if (!state.photos.visible) {
        body.innerHTML =
            `<div class="section-note">Previews are off for speed — use the list (no images) or open Explorer directly.` +
            `<br><button class="btn primary photos-show">View ${g.photo_count} photo(s)</button> ` +
            `<button class="btn photos-folder">Open folder in Explorer</button></div>`;
        body.querySelector(".photos-show").addEventListener("click", () => loadPhotos(1));
        body.querySelector(".photos-folder").addEventListener("click", async () => {
            const res = await revealGroupFolder(g.id);
            if (res) toastSuccess("Opened folder in Explorer.");
        });
        return;
    }
    if (state.photos.loading && state.photos.items.length === 0) {
        body.innerHTML = `<div class="section-note">Loading photos…</div>`;
        return;
    }
    if (state.photos.total === 0) {
        body.innerHTML = state.photos.query
            ? `<div class="section-note">No photos match “${escapeHtml(state.photos.query)}”.</div>`
            : `<div class="section-note">No source photos recorded for this person.</div>`;
        return;
    }
    const perPage = state.photos.mode === "thumbs" ? PHOTOS_PER_PAGE_THUMBS : PHOTOS_PER_PAGE_LIST;
    const pages = Math.max(1, Math.ceil(state.photos.total / perPage));
    body.innerHTML = "";
    body.appendChild(pagerEl(state.photos.page, pages, "photos"));
    if (state.photos.mode === "thumbs") {
        const grid = document.createElement("div");
        grid.className = "photo-grid";
        for (const src of state.photos.items) grid.appendChild(renderPhotoCell(g, src));
        body.appendChild(grid);
    } else {
        const list = document.createElement("div");
        list.className = "photo-rows";
        for (const src of state.photos.items) list.appendChild(renderPhotoRow(g, src));
        body.appendChild(list);
    }
    const foot = document.createElement("div");
    foot.className = "muted small";
    foot.style.marginTop = "10px";
    foot.textContent = state.photos.mode === "list"
        ? "List mode loads no images — click Preview to view one photo, or Reveal to open Explorer."
        : "Thumbnail mode loads only this page — use List for thousands of photos.";
    body.appendChild(foot);
    wirePager(body, "photos", pages, (p) => loadPhotos(p));
}

function renderPhotoRow(g, srcPath) {
    const name = basename(srcPath);
    const row = document.createElement("div");
    row.className = "photo-row";
    row.title = srcPath;
    row.innerHTML =
        `<span class="photo-ico">🖼</span>` +
        `<span class="photo-meta"><span class="photo-name">${escapeHtml(name)}</span>` +
        `<span class="photo-path">${escapeHtml(srcPath)}</span></span>` +
        `<span class="photo-actions"><button class="btn preview">Preview</button>` +
        `<button class="btn reveal">Reveal</button></span>`;
    const preview = () => openSourceViewer(g, state.photos.items, state.photos.items.indexOf(srcPath), state.photos.total);
    row.addEventListener("click", (e) => {
        if (e.target.closest("button")) return;
        preview();
    });
    row.querySelector(".preview").addEventListener("click", preview);
    row.querySelector(".reveal").addEventListener("click", async (e) => {
        e.stopPropagation();
        const res = await revealInExplorer(g.id, srcPath);
        if (res) toastSuccess(`Opened Explorer for “${name}”.`);
    });
    return row;
}

function renderPhotoCell(g, srcPath) {
    const name = basename(srcPath);
    const cell = document.createElement("div");
    cell.className = "photo-cell";
    cell.title = srcPath;
    cell.innerHTML =
        `<img src="${sourceUrl(g.id, srcPath)}" alt="${escapeHtml(name)}" loading="lazy" decoding="async" onerror="this.style.display='none'">` +
        `<div class="photo-foot"><span>${escapeHtml(name)}</span>` +
        `<button class="mini-btn reveal" title="Reveal in Explorer">🗁</button></div>`;
    cell.addEventListener("click", (e) => {
        if (e.target.closest(".reveal")) return;
        openSourceViewer(g, state.photos.items, state.photos.items.indexOf(srcPath), state.photos.total);
    });
    cell.querySelector(".reveal").addEventListener("click", async (e) => {
        e.stopPropagation();
        const res = await revealInExplorer(g.id, srcPath);
        if (res) toastSuccess(`Opened Explorer for “${name}”.`);
    });
    return cell;
}

/* ---------- Face tiles, selection, bulk, drag & drop ---------- */

const keyOf = (groupId, filename) => `${groupId}/${filename}`;

function parseKey(key) {
    const idx = key.indexOf("/");
    return { group_id: Number(key.slice(0, idx)), filename: key.slice(idx + 1) };
}

function pruneSelection() {
    const validGroups = new Set(state.groups.map((g) => g.id));
    for (const key of [...state.selection]) {
        if (!validGroups.has(parseKey(key).group_id)) state.selection.delete(key);
    }
}

function toggleSelect(key) {
    if (state.selection.has(key)) state.selection.delete(key);
    else state.selection.add(key);
    state.lastSelectedKey = key;
    updateBulkBar();
}

function selectRange(groupId, filenames, upToKey) {
    const fromIdx = filenames.findIndex((f) => keyOf(groupId, f) === state.lastSelectedKey);
    const toIdx = filenames.findIndex((f) => keyOf(groupId, f) === upToKey);
    if (fromIdx === -1 || toIdx === -1) { toggleSelect(upToKey); return; }
    const [lo, hi] = [Math.min(fromIdx, toIdx), Math.max(fromIdx, toIdx)];
    for (let i = lo; i <= hi; i++) state.selection.add(keyOf(groupId, filenames[i]));
    updateBulkBar();
}

function renderFaceTile(g, filename) {
    const key = keyOf(g.id, filename);
    const tile = document.createElement("div");
    tile.className = "face-tile" + (state.selection.has(key) ? " selected" : "");
    tile.tabIndex = 0;
    tile.draggable = true;
    tile.dataset.key = key;
    tile.dataset.groupId = g.id;
    tile.dataset.filename = filename;
    tile.innerHTML =
        `<img src="${faceUrl(g.id, filename)}" alt="${escapeHtml(filename)}" loading="lazy" decoding="async">` +
        `<div class="face-tag" title="${escapeHtml(filename)}">${escapeHtml(filename)}</div>` +
        `<div class="tile-btns"><button class="move-btn" title="Move to another person">⇄</button>` +
        `<button class="delete-btn" title="Remove this face">✕</button></div>`;

    tile.querySelector("img").addEventListener("click", (e) => {
        if (e.ctrlKey || e.metaKey) { toggleSelect(key); syncTile(tile); }
        else if (e.shiftKey) { selectRange(g.id, state.faces.items, key); resyncTiles(); }
        else openFaceViewer(g, state.faces.items, state.faces.items.indexOf(filename), state.faces.total);
    });
    tile.querySelector(".delete-btn").addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!confirm(`Remove “${filename}”?\nThe face will be moved to the trash folder.`)) return;
        const result = await deleteFaces([{ group_id: g.id, filename }]);
        if (!result) return;
        toastSuccess("Face moved to trash. Use Undo to restore.");
        loadGroups();
    });
    tile.querySelector(".move-btn").addEventListener("click", (e) => {
        e.stopPropagation();
        tile.dispatchEvent(new CustomEvent("request-move", { bubbles: true }));
    });
    tile.addEventListener("dragstart", (e) => {
        if (!state.selection.has(key)) {
            state.selection.clear();
            state.selection.add(key);
            updateBulkBar();
            resyncTiles();
        }
        e.dataTransfer.setData("text/plain", JSON.stringify([...state.selection].map(parseKey)));
        e.dataTransfer.effectAllowed = "move";
        tile.classList.add("dragging");
    });
    tile.addEventListener("dragend", () => {
        tile.classList.remove("dragging");
        document.querySelectorAll(".drop-target").forEach((c) => c.classList.remove("drop-target"));
    });
    return tile;
}

function syncTile(tile) {
    tile.classList.toggle("selected", state.selection.has(tile.dataset.key));
}

function resyncTiles() {
    document.querySelectorAll("#person-detail .face-tile").forEach(syncTile);
    updateBulkBar();
}

function selectedItems() { return [...state.selection].map(parseKey); }

function clearSelection() {
    state.selection.clear();
    resyncTiles();
}

function updateBulkBar() {
    const bar = $("#bulk-bar");
    if (!bar) return;
    bar.classList.toggle("hidden", state.selection.size === 0);
    $("#bulk-count").textContent = `${state.selection.size} selected`;
    const select = $("#bulk-target-select");
    select.innerHTML = "";
    for (const grp of state.groups) {
        const opt = document.createElement("option");
        opt.value = grp.id;
        opt.textContent = `${grp.name} (${grp.face_count})`;
        select.appendChild(opt);
    }
    select.disabled = state.groups.length <= 1;
}

async function bulkMove() {
    const items = selectedItems();
    if (!items.length) return;
    const useExisting = document.querySelector('input[name="bulk-target"]:checked').value === "existing";
    const result = await moveFaces(
        items,
        useExisting ? parseInt($("#bulk-target-select").value, 10) : -1,
        $("#bulk-new-group-name").value.trim(),
    );
    if (!result) return;
    clearSelection();
    toastSuccess(`Moved ${result.moved} face(s).` + photosSuffix(result));
    loadGroups();
}

async function bulkDelete() {
    const items = selectedItems();
    if (!items.length) return;
    if (!confirm(`Move ${items.length} face(s) to the trash folder?`)) return;
    const result = await deleteFaces(items);
    if (!result) return;
    clearSelection();
    toastSuccess(`${result.deleted} face(s) moved to trash.`);
    loadGroups();
}

async function handleDrop(e, targetGroup) {
    let keys;
    try { keys = JSON.parse(e.dataTransfer.getData("text/plain")); } catch (_) { return; }
    if (!Array.isArray(keys)) return;
    const items = keys.filter((it) => it.group_id !== targetGroup.id);
    if (!items.length) return;
    const result = await moveFaces(items, targetGroup.id, "");
    if (!result) return;
    clearSelection();
    toastSuccess(`Moved ${result.moved} face(s) to ${targetGroup.name}.` + photosSuffix(result));
    loadGroups();
}

async function approveAll() {
    const pending = state.groups.filter((g) => g.face_count > 0);
    if (!pending.length) { toastError("No pending crops to approve."); return; }
    const totalCrops = pending.reduce((s, g) => s + g.face_count, 0);
    if (!confirm(`Approve all ${pending.length} group(s)?\n\nThis PERMANENTLY deletes ${totalCrops} cropped face file(s).`)) return;
    const btn = $("#approve-all-btn");
    btn.disabled = true;
    const result = await approveAllGroups();
    btn.disabled = false;
    if (!result) return;
    toastSuccess(`Approved ${result.groups} group(s) — ${result.deleted} crop(s) deleted.`);
    (externalRefresh || loadGroups)();
}
