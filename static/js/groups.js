/* Groups listing: filters, sorting, pagination, selection, drag & drop, bulk bar */

import {
    $,
    api,
    PAGE_SIZE,
    escapeHtml,
    faceUrl,
    state,
} from "./core.js";
import { toastSuccess } from "./toast.js";
import { deleteFaces, moveFaces, renameGroup } from "./ops.js";
import { openLightbox } from "./lightbox.js";

let refresh = () => {};

export function initGroups(loadGroups) {
    refresh = loadGroups;

    $("#filter-text").addEventListener("input", (e) => {
        state.query = e.target.value.trim();
        state.page = 1;
        renderGroups();
    });
    $("#filter-min-faces").addEventListener("input", (e) => {
        state.minFaces = parseInt(e.target.value, 10) || 0;
        state.page = 1;
        renderGroups();
    });
    $("#sort-by").addEventListener("change", (e) => {
        state.sortBy = e.target.value;
        renderGroups();
    });
    $("#page-prev").addEventListener("click", () => {
        state.page -= 1;
        renderGroups();
    });
    $("#page-next").addEventListener("click", () => {
        state.page += 1;
        renderGroups();
    });

    $("#bulk-move-btn").addEventListener("click", bulkMove);
    $("#bulk-delete-btn").addEventListener("click", bulkDelete);
    $("#bulk-clear-btn").addEventListener("click", clearSelection);
}

/* ---------- Data loading ---------- */

export async function loadGroups() {
    let data;
    try {
        data = await api("/api/groups");
    } catch (err) {
        toastError(`Failed to load groups: ${err.message}`);
        return;
    }
    state.groups = data.groups;
    pruneSelection();
    renderGroups();
    loadStats();
}

async function loadStats() {
    let stats;
    try {
        stats = await api("/api/stats");
    } catch (_) {
        return;
    }
    $("#stats-panel").classList.toggle("hidden", stats.groups === 0);
    if (stats.groups === 0) return;

    $("#stats-summary").innerHTML = `
        <span class="chip">${stats.photos} photos processed</span>
        <span class="chip">${stats.faces} faces total</span>
        <span class="chip">${stats.groups} groups</span>
    `;
    $("#stats-largest").innerHTML = stats.largest
        .map((g) => `<li>${escapeHtml(g.name)} — ${g.count} face(s)</li>`)
        .join("");
}

/* ---------- Filter / sort / pagination ---------- */

function filteredGroups() {
    const q = state.query.toLowerCase();
    const list = state.groups.filter((g) => {
        if (g.faces.length < state.minFaces) return false;
        if (!q) return true;
        return (
            g.name.toLowerCase().includes(q) ||
            g.directory.toLowerCase().includes(q) ||
            g.image_paths.some((p) => p.toLowerCase().includes(q))
        );
    });

    const byName = (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
    switch (state.sortBy) {
        case "name":
            list.sort(byName);
            break;
        case "size-desc":
            list.sort((a, b) => b.faces.length - a.faces.length || byName(a, b));
            break;
        case "size-asc":
            list.sort((a, b) => a.faces.length - b.faces.length || byName(a, b));
            break;
        case "newest":
            list.sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
            break;
        default:
            list.sort((a, b) => a.id - b.id);
    }
    return list;
}

function renderPagination(totalItems) {
    const pages = Math.max(1, Math.ceil(totalItems / PAGE_SIZE));
    state.page = Math.min(Math.max(1, state.page), pages);
    $("#pagination").classList.toggle("hidden", pages <= 1);
    $("#page-info").textContent = `Page ${state.page} of ${pages}`;
    $("#page-prev").disabled = state.page <= 1;
    $("#page-next").disabled = state.page >= pages;
}

/* ---------- Rendering ---------- */

function renderGroups() {
    const container = $("#groups-container");
    container.innerHTML = "";

    const filtered = filteredGroups();
    renderPagination(filtered.length);

    const emptyMsg = $("#groups-empty");
    emptyMsg.classList.toggle("hidden", filtered.length > 0);
    emptyMsg.textContent =
        state.groups.length === 0
            ? 'No groups yet. Run the grouping or click "Refresh groups".'
            : filtered.length === 0
              ? "No groups match the current filter."
              : "";

    const start = (state.page - 1) * PAGE_SIZE;
    for (const group of filtered.slice(start, start + PAGE_SIZE)) {
        container.appendChild(renderGroupCard(group));
    }

    updateBulkBar();
}

function renderGroupCard(group) {
    const card = document.createElement("div");
    card.className = "group-card";
    card.dataset.groupId = group.id;

    const header = document.createElement("div");
    header.className = "group-header";
    header.innerHTML = `
        <input type="text" class="group-name" value="${escapeHtml(group.name)}"
               title="Person name (stored in database)">
        <span class="face-count">${group.faces.length} face(s)</span>
        <button class="rename-btn">Save name</button>
        <button class="copy-paths-btn" title="Copy source photo paths to clipboard">Copy paths</button>
    `;
    card.appendChild(header);

    wireRename(header, group);
    wireCopyPaths(header, group);

    const grid = document.createElement("div");
    grid.className = "faces-grid";

    if (group.faces.length === 0) {
        grid.innerHTML = '<p class="empty-group">No cropped faces in this group.</p>';
    }

    for (const filename of group.faces) {
        grid.appendChild(renderFaceTile(group, filename));
    }
    card.appendChild(grid);

    /* Drop target for drag-and-drop moves */
    card.addEventListener("dragover", (e) => {
        e.preventDefault();
        card.classList.add("drag-over");
    });
    card.addEventListener("dragleave", () => card.classList.remove("drag-over"));
    card.addEventListener("drop", (e) => {
        e.preventDefault();
        card.classList.remove("drag-over");
        handleDrop(e, group);
    });

    return card;
}

function wireRename(header, group) {
    const renameBtn = header.querySelector(".rename-btn");
    const nameInput = header.querySelector(".group-name");

    const save = async () => {
        const newName = nameInput.value.trim();
        if (!newName || newName === group.name) return;
        const result = await renameGroup(group.id, newName);
        if (!result) {
            nameInput.value = group.name;
            return;
        }
        group.name = newName;
        nameInput.blur();
        toastSuccess(`Renamed to "${newName}".`);
    };

    renameBtn.addEventListener("click", save);
    nameInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") save();
    });
}

async function wireCopyPaths(header, group) {
    const btn = header.querySelector(".copy-paths-btn");
    btn.addEventListener("click", async () => {
        try {
            await navigator.clipboard.writeText(group.image_paths.join("\n"));
            btn.textContent = "Copied!";
            setTimeout(() => (btn.textContent = "Copy paths"), 1200);
        } catch (err) {
            toastError(`Could not copy: ${err.message}`);
        }
    });
}

/* ---------- Face tiles ---------- */

const keyOf = (groupId, filename) => `${groupId}/${filename}`;

function parseKey(key) {
    const idx = key.indexOf("/");
    return { group_id: Number(key.slice(0, idx)), filename: key.slice(idx + 1) };
}

function pruneSelection() {
    const valid = new Set(
        state.groups.flatMap((g) => g.faces.map((f) => keyOf(g.id, f))),
    );
    for (const key of [...state.selection]) {
        if (!valid.has(key)) state.selection.delete(key);
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
    if (fromIdx === -1 || toIdx === -1) {
        toggleSelect(upToKey);
        return;
    }
    const [lo, hi] = [Math.min(fromIdx, toIdx), Math.max(fromIdx, toIdx)];
    for (let i = lo; i <= hi; i++) {
        state.selection.add(keyOf(groupId, filenames[i]));
    }
    updateBulkBar();
}

function renderFaceTile(group, filename) {
    const key = keyOf(group.id, filename);

    const tile = document.createElement("div");
    tile.className = "face-tile";
    tile.tabIndex = 0;
    tile.draggable = true;
    tile.dataset.key = key;
    tile.dataset.groupId = group.id;
    tile.dataset.filename = filename;
    if (state.selection.has(key)) tile.classList.add("selected");

    tile.innerHTML = `
        <img src="${faceUrl(group, filename)}"
             alt="${escapeHtml(filename)}" loading="lazy">
        <div class="face-name" title="${escapeHtml(filename)}">${escapeHtml(filename)}</div>
        <div class="tile-actions">
            <button class="move-btn" title="Move to another group">&#8646;</button>
            <button class="delete-btn" title="Remove this face">&#10005;</button>
        </div>
    `;

    tile.querySelector("img").addEventListener("click", (e) => {
        if (e.ctrlKey || e.metaKey) {
            toggleSelect(key);
            syncTileClasses(tile);
        } else if (e.shiftKey) {
            selectRange(group.id, group.faces, key);
            resyncAllTiles();
        } else {
            openLightbox(group, group.faces.indexOf(filename));
        }
    });

    tile.querySelector(".delete-btn").addEventListener("click", async () => {
        if (!confirm(`Remove "${filename}"?\nThe face will be moved to the trash folder.`)) return;
        const result = await deleteFaces([{ group_id: group.id, filename }]);
        if (!result) return;
        toastSuccess("Face moved to trash. Use Undo to restore.");
        refresh();
    });

    tile.querySelector(".move-btn").addEventListener("click", () => {
        // handled via injected callback set by main.js through initGroups' module graph
        tile.dispatchEvent(new CustomEvent("request-move", { bubbles: true }));
    });

    tile.addEventListener("dragstart", (e) => {
        if (!state.selection.has(key)) {
            state.selection.clear();
            state.selection.add(key);
            updateBulkBar();
            resyncAllTiles();
        }
        e.dataTransfer.setData(
            "text/plain",
            JSON.stringify([...state.selection].map(parseKey)),
        );
        e.dataTransfer.effectAllowed = "move";
        tile.classList.add("dragging");
    });
    tile.addEventListener("dragend", () => {
        tile.classList.remove("dragging");
        document
            .querySelectorAll(".group-card.drag-over")
            .forEach((c) => c.classList.remove("drag-over"));
    });

    return tile;
}

function syncTileClasses(tile) {
    tile.classList.toggle("selected", state.selection.has(tile.dataset.key));
}

function resyncAllTiles() {
    document.querySelectorAll(".face-tile").forEach(syncTileClasses);
    updateBulkBar();
}

/* ---------- Bulk bar ---------- */

function selectedItems() {
    return [...state.selection].map(parseKey);
}

function clearSelection() {
    state.selection.clear();
    resyncAllTiles();
}

function updateBulkBar() {
    const bar = $("#bulk-bar");
    bar.classList.toggle("hidden", state.selection.size === 0);
    $("#bulk-count").textContent = `${state.selection.size} selected`;

    const select = $("#bulk-target-select");
    select.innerHTML = "";
    for (const g of state.groups) {
        const opt = document.createElement("option");
        opt.value = g.id;
        opt.textContent = `${g.name} (${g.faces.length})`;
        select.appendChild(opt);
    }
    select.disabled = state.groups.length <= 1;
}

async function bulkMove() {
    const items = selectedItems();
    if (items.length === 0) return;

    const useExisting = document.querySelector(
        'input[name="bulk-target"]:checked',
    ).value === "existing";

    const result = await moveFaces(
        items,
        useExisting ? parseInt($("#bulk-target-select").value, 10) : -1,
        $("#bulk-new-group-name").value.trim(),
    );
    if (!result) return;

    clearSelection();
    toastSuccess(`Moved ${result.moved} face(s).`);
    refresh();
}

async function bulkDelete() {
    const items = selectedItems();
    if (items.length === 0) return;
    if (!confirm(`Move ${items.length} face(s) to the trash folder?`)) return;

    const result = await deleteFaces(items);
    if (!result) return;

    clearSelection();
    toastSuccess(`${result.deleted} face(s) moved to trash.`);
    refresh();
}

/* ---------- Drag & drop ---------- */

async function handleDrop(e, targetGroup) {
    let keys;
    try {
        keys = JSON.parse(e.dataTransfer.getData("text/plain"));
    } catch (_) {
        return;
    }
    if (!Array.isArray(keys)) return;

    const items = keys.filter((it) => it.group_id !== targetGroup.id);
    if (items.length === 0) return;

    const result = await moveFaces(items, targetGroup.id, "");
    if (!result) return;

    clearSelection();
    toastSuccess(`Moved ${result.moved} face(s) to ${targetGroup.name}.`);
    refresh();
}
