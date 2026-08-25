/* Lightbox: enlarged face + source photos */

import { $, state, faceUrl, sourceUrl } from "./core.js";
import { deleteFaces } from "./ops.js";
import { openMoveModal } from "./movemodal.js";

let refresh = () => {};

export function initLightbox(loadGroups) {
    refresh = loadGroups;

    $("#lightbox-close").addEventListener("click", closeLightbox);
    $("#lightbox-delete").addEventListener("click", lightboxDelete);
    $("#lightbox-move").addEventListener("click", () => {
        const lb = state.lightbox;
        if (!lb) return;
        const filename = lb.group.faces[lb.index];
        const groupId = lb.group.id;
        closeLightbox();
        const group = state.groups.find((g) => g.id === groupId);
        if (group) openMoveModal(group, filename);
    });
    $("#lightbox").addEventListener("click", (e) => {
        if (e.target === e.currentTarget) closeLightbox();
    });
}

export function openLightbox(group, index) {
    state.lightbox = { group, index };
    renderFace();
    $("#lightbox").classList.remove("hidden");
}

export function closeLightbox() {
    $("#lightbox").classList.add("hidden");
    state.lightbox = null;
}

export function lightboxIsOpen() {
    return !$("#lightbox").classList.contains("hidden") && state.lightbox !== null;
}

export function lightboxNavigate(delta) {
    const lb = state.lightbox;
    if (!lb) return;
    const len = lb.group.faces.length;
    lb.index = (lb.index + delta + len) % len;
    renderFace();
}

function currentFilename() {
    const lb = state.lightbox;
    return lb ? lb.group.faces[lb.index] : null;
}

async function lightboxDelete() {
    const filename = currentFilename();
    if (!filename) return;
    if (!confirm(`Remove "${filename}"?\nThe face will be moved to the trash folder.`)) return;

    const result = await deleteFaces([{ group_id: state.lightbox.group.id, filename }]);
    if (!result) return;

    closeLightbox();
    refresh();
}

function renderFace() {
    const lb = state.lightbox;
    if (!lb) return;
    const group = lb.group;
    const filename = group.faces[lb.index];
    if (!filename) {
        closeLightbox();
        return;
    }

    $("#lightbox-title").textContent =
        `${group.name} — face ${lb.index + 1} / ${group.faces.length} (${filename})`;
    $("#lightbox-img").src = faceUrl(group, filename);

    const sourcesBox = $("#lightbox-sources");
    sourcesBox.innerHTML = "";
    const sources = group.image_paths;

    if (sources.length === 0) {
        sourcesBox.innerHTML = '<p class="muted">No source photo paths recorded.</p>';
        return;
    }

    const label = document.createElement("p");
    label.className = "muted";
    label.textContent = `Source photo(s) (${sources.length}):`;
    sourcesBox.appendChild(label);

    for (const src of sources) {
        const row = document.createElement("div");
        row.className = "source-row";
        row.innerHTML = `
            <button type="button" class="link-btn view-src"
                    title="View full photo">${escapeAttr(src)}</button>
            <a href="${sourceUrl(group.id, src)}" target="_blank" rel="noopener">open</a>
        `;
        row.querySelector(".view-src").addEventListener("click", () => {
            $("#lightbox-img").src = sourceUrl(group.id, src);
            $("#lightbox-title").textContent = `${group.name} — ${src}`;
        });
        sourcesBox.appendChild(row);
    }
}

function escapeAttr(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}
