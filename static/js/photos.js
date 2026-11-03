/* Photo wall + photo search: every processed photo in one grid, no face
   crops anywhere. Clicking a photo opens the tag viewer: a box per face,
   Facebook-style naming (add/change that face's person).

   Performance notes (built for big libraries):
   - thumbs come from a server-side disk cache at 320px instead of
     re-reading multi-MB originals; the viewer pulls one 1280px version
   - one paged fetch per chunk (60 photos, index-backed SQL), incremental
     "load more", stale-token guarded so typing mid-flight can't corrupt state
   - <img loading="lazy" decoding="async"> inside fixed-ratio clips: zero
     layout shift, browsers skip offscreen fetches entirely
   - hidden pages never request images (lazy loading only starts on view) */

import {
    $, api, basename, escapeHtml, getDbFile, isRunning,
    PHOTOS_PER_PAGE_WALL, shortDir, state, thumbUrl,
} from "./core.js";
import { toastError, toastSuccess } from "./toast.js";

const STACK = {
    photos: { name: "photos", items: [], page: 0, perPage: PHOTOS_PER_PAGE_WALL, total: 0, q: "", loading: false, done: false, rev: 0, tiles: new Map() },
    find: { name: "find", items: [], page: 0, perPage: PHOTOS_PER_PAGE_WALL, total: 0, q: "", loading: false, done: false, rev: 0, tiles: new Map() },
};

let pv = null; // active viewer state {path, kind, list, index, tags, duplicateOf, busy}

/* ---------- init ---------- */

export function initPhotos() {
    for (const key of ["photos", "find"]) {
        const w = STACK[key];
        w.grid = $(`#${key}-grid`);
        w.empty = $(`#${key}-empty`);
        w.info = $(`#${key}-info`);
        w.more = $(`#${key}-loadmore`);
        w.sentry = $(`#${key}-sentry`);
        w.more?.addEventListener("click", () => loadNext(w));
        w.observer = new IntersectionObserver((entries) => {
            if (!entries.some((e) => e.isIntersecting)) return;
            if ($(`#page-${key}`)?.classList.contains("hidden")) return;
            loadNext(w);
        }, { rootMargin: "700px 0px" });
        if (w.sentry) w.observer.observe(w.sentry);
        // Grid clicks (delegated) open the viewer.
        w.grid.addEventListener("click", (e) => {
            const tile = e.target.closest(".wall-tile");
            if (!tile) return;
            openViewer(w, tile.dataset.path);
        });
    }

    const findInput = $("#find-input");
    let findTimer = null;
    findInput.addEventListener("input", () => {
        $("#find-clear").classList.toggle("hidden", !findInput.value);
        clearTimeout(findTimer);
        findTimer = setTimeout(() => {
            if (findInput.value.trim() !== STACK.find.q || !STACK.find.page) searchPhotos();
        }, 280);
    });
    findInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { clearTimeout(findTimer); searchPhotos(); }
    });
    findInput.addEventListener("search", () => { clearTimeout(findTimer); searchPhotos(); });
    $("#find-clear").addEventListener("click", () => {
        findInput.value = "";
        $("#find-clear").classList.add("hidden");
        searchPhotos();
        findInput.focus();
    });

    // Populate lazily when a page first opens; refresh on data changes.
    document.addEventListener("page-shown", (e) => {
        const name = e.detail?.name;
        if (name === "photos" || name === "find") ensureLoaded(STACK[name]);
    });
    document.addEventListener("groups-refreshed", () => {
        if (!pv) resetIfVisible("photos");
        if (!pv) resetIfVisible("find");
    });

    // Viewer controls.
    $("#photos-refresh").addEventListener("click", () => hardReload(STACK.photos));
    $("#pv-close").addEventListener("click", closePhotoViewer);
    $("#pv-prev").addEventListener("click", () => viewerNavigate(-1));
    $("#pv-next").addEventListener("click", () => viewerNavigate(1));
    $("#pv-img").addEventListener("load", sizeViewerFrame);
    $("#photo-viewer").addEventListener("click", (e) => {
        if (e.target === e.currentTarget) closePhotoViewer();
    });
    // Click anywhere else in the photo dismisses the naming popover.
    document.addEventListener("pointerdown", (e) => {
        const pop = $("#pv-pop");
        if (!pop || pop.classList.contains("hidden")) return;
        if (pop.contains(e.target)) return;
        if (e.target.closest && e.target.closest(".pv-tag")) return;
        closePopover();
    });
    // Keep the photo fitted when the window is resized while the viewer is open.
    window.addEventListener("resize", () => {
        if (pv) sizeViewerFrame();
    });
}

function resetIfVisible(key) {
    const w = STACK[key];
    if ($(`#page-${key}`)?.classList.contains("hidden")) return;
    hardReload(w);
}

function ensureLoaded(w) {
    if (!w.page && !w.loading) hardReload(w);
}

/* ---------- wall loading ---------- */

function setSearch(w, q) {
    w.q = q;
    w.rev += 1;
    w.items.length = 0; // keep array identity: an open viewer reads this list
    w.tiles.clear();
    w.page = 0;
    w.total = 0;
    w.done = false;
    w.grid.replaceChildren();
}

function hardReload(w) {
    setSearch(w, w.q);
    loadNext(w);
}

async function loadNext(w) {
    if (w.loading || (w.done && w.page > 0)) return;
    w.loading = true;
    w.grid.classList.add("wall-loading");
    w.more.classList.add("hidden");
    const rev = w.rev;
    const page = w.page + 1;
    try {
        const data = await api(
            `/api/photos?page=${page}&per_page=${w.perPage}` +
            (w.q ? `&q=${encodeURIComponent(w.q)}` : "")
        );
        if (rev !== w.rev) return; // query changed mid-flight: drop the reply
        w.page = page;
        w.total = data.total || 0;
        const fresh = data.photos || [];
        w.items.push(...fresh);
        w.done = !fresh.length || w.items.length >= w.total;
        appendTiles(w, fresh);
        renderEmpty(w);
        renderInfo(w);
    } catch (err) {
        toastError(err.message);
    } finally {
        const stale = rev !== w.rev; // query raced mid-flight: reply was dropped
        w.loading = false;
        w.grid.classList.remove("wall-loading");
        w.more.classList.toggle("hidden", w.done);
        w.more.disabled = w.loading;
        if (stale) {
            // The query changed while this request ran: setSearch reset state
            // and its own reload was swallowed by `w.loading`, so the current
            // query would otherwise never be fetched for a lost scroll event.
            loadNext(w);
        }
    }
}

function renderEmpty(w) {
    w.empty.classList.toggle("hidden", w.items.length > 0);
    if (w.items.length) return;
    const h3 = w.empty.querySelector("h3");
    const p = w.empty.querySelector("p");
    if (w.name === "find") {
        h3.textContent = w.q ? `No photos match “${w.q}”` : "Type to search photos";
        p.textContent = w.q
            ? "Search matches photo file names and the people tagged in them."
            : "Search by photo name or person name.";
    } else {
        h3.textContent = "No photos yet";
        p.textContent =
            "Run grouping on a folder first — every processed photo then shows up here.";
    }
}

function renderInfo(w) {
    const label = w.name === "find" ? "match" : "photo";
    w.info.textContent = w.items.length
        ? `${w.items.length.toLocaleString()} of ${w.total.toLocaleString()} ${label}${w.total === 1 ? "" : "s"}`
        : "";
}

/* ---------- tiles ---------- */

function chipsHtml(item) {
    const chips = item.people.slice(0, 3).map((p) =>
        `<span class="wall-chip" title="${escapeHtml(p.name)}">${escapeHtml(p.name)}</span>`
    );
    if (item.people.length > 3) {
        const rest = item.people.slice(3).map((p) => p.name).join(", ");
        chips.push(`<span class="wall-chip more" title="${escapeHtml(rest)}">+${item.people.length - 3}</span>`);
    }
    if (item.unnamed > 0) {
        chips.push(`<span class="wall-chip unnamed" title="${item.unnamed} face${item.unnamed === 1 ? "" : "s"} not named yet">＋${item.unnamed}</span>`);
    }
    return chips.join("");
}

function paintTileOverlays(tile, item) {
    const dup = item.dup_of
        ? `<span class="wall-flag" title="Exact duplicate of ${escapeHtml(basename(item.dup_of))}">⧉</span>`
        : "";
    const count = item.faces > 0
        ? `<span class="wall-facecount" title="${item.faces} tagged face${item.faces === 1 ? "" : "s"}">👤 ${item.faces}</span>`
        : "";
    tile.querySelector(".wall-flags").replaceChildren();
    tile.querySelector(".wall-flags").innerHTML = dup + count;
    tile.querySelector(".wall-chips").innerHTML = chipsHtml(item);
}

function appendTiles(w, items) {
    const frag = document.createDocumentFragment();
    for (const item of items) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "wall-tile";
        btn.dataset.path = item.path;
        btn.setAttribute("aria-label", `Open ${basename(item.path)}`);
        btn.title = basename(item.path);
        btn.innerHTML =
            `<img loading="lazy" decoding="async" src="${thumbUrl(item.path, 320)}" alt="">` +
            `<span class="wall-flags"></span><span class="wall-chips"></span>`;
        w.tiles.set(item.path, btn);
        paintTileOverlays(btn, item);
        frag.appendChild(btn);
    }
    w.grid.appendChild(frag);
}

/* One tagging changed this photo → rebuild its chips in both grids. */
function updateTileChips(path, tags) {
    const itemView = tagsToItem(tags);
    for (const w of [STACK.photos, STACK.find]) {
        const idx = w.items.findIndex((i) => i.path === path);
        if (idx >= 0) w.items[idx] = { ...w.items[idx], ...itemView };
        const tile = w.tiles.get(path);
        if (tile) paintTileOverlays(tile, w.items[idx] || itemView);
    }
}

function tagsToItem(tags) {
    const people = [];
    let unnamed = 0;
    for (const t of tags) {
        if (t.group_id) {
            if (!people.some((p) => p.id === t.group_id)) {
                people.push({ id: t.group_id, name: t.name || `Person ${t.group_id}` });
            }
        } else {
            unnamed += 1;
        }
    }
    people.sort((a, b) => a.name.localeCompare(b.name));
    return { faces: tags.length, people, unnamed };
}

/* ---------- photo search ---------- */

function searchPhotos() {
    const q = $("#find-input").value.trim();
    setSearch(STACK.find, q);
    loadNext(STACK.find);
}

/* ---------- tag viewer (Facebook-style naming) ---------- */

export function photoViewerIsOpen() {
    return pv !== null && !$("#photo-viewer").classList.contains("hidden");
}

export function closePhotoViewer() {
    $("#photo-viewer").classList.add("hidden");
    closePopover();
    pv = null;
}

function openViewer(w, path) {
    const index = w.items.findIndex((i) => i.path === path);
    pv = {
        kind: w.name,
        list: w.items,
        index: index < 0 ? 0 : index,
        tags: [],
        duplicateOf: null,
        busy: false,
        path,
    };
    $("#photo-viewer").classList.remove("hidden");
    $("#pv-meta").replaceChildren();
    loadPhoto(path);
}

function viewerNavigate(delta) {
    if (!pv || pv.busy || !pv.list.length) return;
    pv.index = (pv.index + delta + pv.list.length) % pv.list.length;
    const next = pv.list[pv.index];
    closePopover();
    if (next) loadPhoto(next.path);
}

function preload(path) {
    const img = new Image();
    img.decoding = "async";
    img.src = thumbUrl(path, 1280);
}

async function loadPhoto(path) {
    if (!pv) return;
    pv.busy = true;
    pv.path = path;
    $("#pv-title").textContent = basename(path);
    const img = $("#pv-img");
    img.removeAttribute("src");
    $("#pv-loading").classList.remove("hidden");
    const frame = $("#pv-frame");
    frame.querySelector(".pv-tag-layer")?.remove();
    frame.style.width = "";
    frame.style.height = "";
    $("#pv-counter").textContent = "";

    img.decoding = "async";
    img.alt = basename(path);
    img.src = thumbUrl(path, 1280);

    let tags = [], duplicateOf = null;
    try {
        const data = await api(`/api/photos/tags?path=${encodeURIComponent(path)}`);
        tags = data.tags || [];
        duplicateOf = data.duplicate_of || null;
    } catch (err) {
        toastError(err.message);
    }
    if (!pv || pv.path !== path || !photoViewerIsOpen()) return; // moved on meanwhile
    hidePvLoading(); // this load owns the spinner; fetch failed or finished
    pv.tags = tags;
    pv.duplicateOf = duplicateOf;
    pv.busy = false;

    renderBoxes();
    renderViewerMeta();
    // Preload neighbours so ←/→ feels instant.
    const prev = pv.list[(pv.index - 1 + pv.list.length) % pv.list.length];
    const next = pv.list[(pv.index + 1) % pv.list.length];
    if (prev) preload(prev.path);
    if (next) preload(next.path);
}

/* Fit the photo to the stage while keeping the box layer (calibrated on the
   image element itself) aligned: the frame is given exact pixel size. */
function sizeViewerFrame() {
    if (!pv) return;
    const stage = $("#pv-stage");
    const img = $("#pv-img");
    if (!img.naturalWidth) return;
    const availW = Math.max(120, stage.clientWidth - 8);
    const availH = Math.max(200, stage.clientHeight - 8);
    const scale = Math.min(availW / img.naturalWidth, availH / img.naturalHeight, 1.4);
    const frame = $("#pv-frame");
    frame.style.width = `${Math.round(img.naturalWidth * scale)}px`;
    frame.style.height = `${Math.round(img.naturalHeight * scale)}px`;
}

function labelFor(tag) {
    if (tag.group_id == null) return null;
    return tag.name || `Person ${tag.group_id}`;
}

function renderBoxes() {
    const frame = $("#pv-frame");
    frame.querySelector(".pv-tag-layer")?.remove();
    $("#pv-counter").textContent = renderCounter();
    if (!pv || !pv.tags.length) return;
    const layer = document.createElement("div");
    layer.className = "pv-tag-layer";
    pv.tags.forEach((t, i) => {
        const name = labelFor(t);
        const [x1, y1, x2, y2] = t.bbox || [0, 0, 0, 0];
        const el = document.createElement("button");
        el.type = "button";
        el.className = `pv-tag${name ? "" : " unnamed"}`;
        el.style.left = `${(x1 * 100).toFixed(3)}%`;
        el.style.top = `${(y1 * 100).toFixed(3)}%`;
        el.style.width = `${((x2 - x1) * 100).toFixed(3)}%`;
        el.style.height = `${((y2 - y1) * 100).toFixed(3)}%`;
        el.innerHTML = `<span class="pv-tag-label">${name ? escapeHtml(name) : "+ Add name"}</span>`;
        el.setAttribute("aria-label", name ? `${name} — change person` : "Add a name to this face");
        el.addEventListener("click", (e) => {
            e.stopPropagation();
            openPopover(t, i, el);
        });
        layer.appendChild(el);
    });
    frame.appendChild(layer);
}

function renderCounter() {
    if (!pv) return "";
    const named = pv.tags.filter((t) => t.group_id != null).length;
    const parts = [];
    if (pv.tags.length) {
        parts.push(`${pv.tags.length} face${pv.tags.length === 1 ? "" : "s"}`);
        parts.push(`${named} named${pv.tags.length - named ? `, ${pv.tags.length - named} unnamed` : ""}`);
    } else {
        parts.push("no faces were detected in this photo");
    }
    if (pv.list.length > 1) parts.unshift(`${pv.index + 1} / ${pv.list.length}`);
    return parts.filter(Boolean).join(" · ");
}

function renderViewerMeta() {
    const box = $("#pv-meta");
    box.replaceChildren();
    if (!pv) return;
    const bar = document.createElement("div");
    bar.className = "meta-bar";
    bar.innerHTML =
        `<div class="meta-text">` +
        `<div class="meta-name">${escapeHtml(basename(pv.path))}</div>` +
        `<div class="meta-path" title="${escapeHtml(pv.path)}">${escapeHtml(shortDir(pv.path, 60))}</div>` +
        `</div>`;
    if (pv.duplicateOf) {
        const dupNote = document.createElement("span");
        dupNote.className = "badge";
        dupNote.title = pv.duplicateOf;
        dupNote.textContent = `duplicate of ${basename(pv.duplicateOf)}`;
        bar.appendChild(dupNote);
    }
    const copyBtn = document.createElement("button");
    copyBtn.className = "btn";
    copyBtn.textContent = "⧉ Copy path";
    copyBtn.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(pv.path); toastSuccess("Path copied."); }
        catch (_) { toastSuccess(pv.path); }
    });
    const revealBtn = document.createElement("button");
    revealBtn.className = "btn";
    revealBtn.textContent = "🗁 Reveal";
    revealBtn.addEventListener("click", async () => {
        try {
            await api("/api/reveal", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ path: pv.path, db_file: getDbFile() }),
            });
            toastSuccess("Opened in Explorer.");
        } catch (err) { toastError(err.message); }
    });
    bar.appendChild(copyBtn);
    bar.appendChild(revealBtn);
    box.appendChild(bar);
}

/* ---------- naming popover ---------- */

function closePopover() {
    $("#pv-pop")?.classList.add("hidden");
}

function openPopover(tag, facePos, anchorEl) {
    if (isRunning()) {
        toastError("A grouping run is in progress — curation unlocks when it finishes.");
        return;
    }
    const pop = $("#pv-pop");
    const name = labelFor(tag);
    pop.classList.remove("hidden");
    pop.innerHTML =
        `<div class="pv-pop-head">Face ${facePos + 1}${name ? ` · ${escapeHtml(name)}` : ""}</div>` +
        `<input class="pv-pop-input" placeholder="Type a name, or filter below…" autocomplete="off" autocapitalize="off">` +
        `<div class="pv-pop-create hidden"></div>` +
        `<div class="pv-pop-list"></div>`;
    const input = pop.querySelector(".pv-pop-input");
    const list = pop.querySelector(".pv-pop-list");
    const create = pop.querySelector(".pv-pop-create");

    // Position below the face box (flip above when near the bottom edge),
    // clamped to the stage so it never pokes outside.
    const stage = $("#pv-stage");
    const sr = stage.getBoundingClientRect();
    const ar = anchorEl.getBoundingClientRect();
    const popW = Math.min(300, Math.max(220, sr.width));
    let left = Math.round(Math.max(8, Math.min(ar.left - sr.left + ar.width / 2 - popW / 2, sr.width - popW - 8)));
    let top = Math.round(ar.bottom - sr.top + 8);
    let flip = false;
    if (top + 300 > sr.height) {
        top = Math.max(6, Math.round(ar.top - sr.top - 8));
        flip = true;
    }
    pop.style.width = `${popW}px`;
    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;
    pop.style.transform = flip ? "translateY(-101%)" : "none";

    const peopleAll = state.groups.map((g) => ({
        id: g.id,
        name: g.name || g.directory.split(/[\\/]/).pop(),
    }));

    const renderList = () => {
        const q = input.value.trim().toLowerCase();
        const matches = peopleAll
            .filter((p) => !q || p.name.toLowerCase().includes(q))
            .slice(0, 24);
        const exact = q ? peopleAll.find((p) => p.name.toLowerCase() === q) : null;
        list.replaceChildren();
        if (name && (!q || name.toLowerCase().includes(q))) {
            list.appendChild(popItem(`✓ ${name} (current)`, null));
        }
        if (q && !exact) {
            create.textContent = `＋ Create person “${input.value.trim()}”`;
            create.classList.remove("hidden");
            create.onclick = () => assign(tag, facePos, { new_name: input.value.trim() });
        } else {
            create.classList.add("hidden");
            create.onclick = null;
        }
        for (const p of matches) {
            const isCurrent = name && p.id === tag.group_id;
            list.appendChild(popItem(
                isCurrent ? `✓ ${p.name} (current)` : p.name,
                isCurrent ? null : () => assign(tag, facePos, { group_id: p.id }),
            ));
        }
        if (!list.children.length) {
            const note = document.createElement("div");
            note.className = "muted small";
            note.style.padding = "8px 10px";
            note.textContent = peopleAll.length
                ? `No person matches “${input.value.trim()}” — press Enter to create it.`
                : "No people yet — type a name and press Enter to create one.";
            list.appendChild(note);
        }
    };

    input.addEventListener("input", renderList);
    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
            e.preventDefault();
            const q = input.value.trim().toLowerCase();
            const exact = q ? peopleAll.find((p) => p.name.toLowerCase() === q) : null;
            if (exact) assign(tag, facePos, { group_id: exact.id });
            else if (q) assign(tag, facePos, { new_name: input.value.trim() });
        } else if (e.key === "Escape") {
            e.preventDefault();
            e.stopPropagation();
            closePopover();
        }
    });
    input.addEventListener("blur", () => setTimeout(() => {
        if (pop.classList.contains("hidden")) return;
        const active = document.activeElement;
        if (pop.contains(active)) return;
        closePopover();
    }, 150));

    renderList();
    input.focus();
    input.select();
}

function popItem(text, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "pv-pop-item";
    b.textContent = text;
    if (onClick) {
        b.addEventListener("click", onClick);
    } else {
        b.disabled = true;
        b.classList.add("current");
    }
    return b;
}

/* ---------- assignment ---------- */

async function assign(tag, facePos, opts) {
    if (!pv) return;
    const body = { path: pv.path, face_idx: tag.face_idx, db_file: getDbFile() };
    if (opts.group_id != null) body.group_id = opts.group_id;
    else body.new_name = opts.new_name;
    closePopover();
    pv.busy = true;
    $("#pv-loading").classList.remove("hidden");
    try {
        const res = await api("/api/photos/tag", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        if (res.created_person) await refreshPeople();
        toastSuccess(res.created_person
            ? `New person “${res.group.name}” saved.`
            : `Face named “${res.group.name}”.`);
        document.dispatchEvent(new CustomEvent("groups-refreshed"));
        await reloadTagData();
    } catch (err) {
        hidePvLoading();
        toastError(err.message);
    } finally {
        if (pv) pv.busy = false;
    }
}

function hidePvLoading() {
    $("#pv-loading").classList.add("hidden");
}

async function reloadTagData() {
    if (!pv) { hidePvLoading(); return; }
    const path = pv.path;
    try {
        const data = await api(`/api/photos/tags?path=${encodeURIComponent(path)}`);
        if (!pv || pv.path !== path || !photoViewerIsOpen()) { hidePvLoading(); return; }
        pv.tags = data.tags || [];
        pv.duplicateOf = data.duplicate_of || pv.duplicateOf;
        hidePvLoading();
        renderBoxes();
        renderViewerMeta();
        updateTileChips(path, pv.tags);
    } catch (err) {
        hidePvLoading();
        toastError(err.message);
    }
}

async function refreshPeople() {
    try {
        const data = await api("/api/groups?summary=1");
        state.groups = data.groups || [];
    } catch (_) { /* non-fatal */ }
}

/* ---------- keyboard ---------- */

document.addEventListener("keydown", (e) => {
    if (!photoViewerIsOpen()) return;
    if (e.target && ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
    if (e.key === "ArrowRight") { e.preventDefault(); viewerNavigate(1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); viewerNavigate(-1); }
});
