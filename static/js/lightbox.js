/* Single-image viewer: loads only one image at a time (scalable). */

import { $, api, basename, escapeHtml, faceUrl, sourceUrl, state } from "./core.js";
import { deleteFaces, revealInExplorer } from "./ops.js";
import { toastSuccess } from "./toast.js";

let refresh = () => {};

export function initLightbox(loadGroups) {
    refresh = loadGroups;
    $("#lightbox-close").addEventListener("click", closeLightbox);
    $("#lightbox-prev").addEventListener("click", () => lightboxNavigate(-1));
    $("#lightbox-next").addEventListener("click", () => lightboxNavigate(1));
    $("#lightbox-delete").addEventListener("click", lightboxDelete);
    $("#lightbox-move").addEventListener("click", () => {
        const lb = state.lightbox;
        if (!lb || lb.kind !== "face") return;
        const filename = lb.items[lb.index];
        const groupId = lb.groupId;
        closeLightbox();
        const evt = new CustomEvent("request-move-explicit", { detail: { groupId, filename } });
        document.dispatchEvent(evt);
    });
    $("#lightbox").addEventListener("click", (e) => {
        if (e.target === e.currentTarget) closeLightbox();
    });
}

export function openFaceViewer(group, items, index, total) {
    state.lightbox = {
        kind: "face",
        groupId: group.id,
        groupName: group.name,
        items: [...items],
        index: Math.max(0, index),
        total: total ?? items.length,
    };
    renderViewer();
    $("#lightbox").classList.remove("hidden");
}

export function openSourceViewer(group, items, index, total) {
    state.lightbox = {
        kind: "source",
        groupId: group.id,
        groupName: group.name,
        items: [...items],
        index: Math.max(0, index),
        total: total ?? items.length,
    };
    renderViewer();
    $("#lightbox").classList.remove("hidden");
}

// Back-compat aliases used by older call sites
export const openLightbox = openFaceViewer;
export const openSourcePreview = openSourceViewer;

export function closeLightbox() {
    $("#lightbox").classList.add("hidden");
    state.lightbox = null;
}

export function lightboxIsOpen() {
    return !$("#lightbox").classList.contains("hidden") && state.lightbox !== null;
}

export function lightboxNavigate(delta) {
    const lb = state.lightbox;
    if (!lb || lb.items.length === 0) return;
    lb.index = (lb.index + delta + lb.items.length) % lb.items.length;
    renderViewer();
}

async function lightboxDelete() {
    const lb = state.lightbox;
    if (!lb || lb.kind !== "face") return;
    const filename = lb.items[lb.index];
    if (!filename) return;
    if (!confirm(`Remove “${filename}”?\nThe face will be moved to the trash folder.`)) return;
    const result = await deleteFaces([{ group_id: lb.groupId, filename }]);
    if (!result) return;
    toastSuccess("Face moved to trash. Use Undo to restore.");
    closeLightbox();
    refresh();
}

function renderViewer() {
    const lb = state.lightbox;
    if (!lb) return;
    const isFace = lb.kind === "face";
    const current = lb.items[lb.index];
    if (current == null) { closeLightbox(); return; }

    const name = isFace ? current : basename(current);
    $("#lightbox-title").textContent = `${lb.groupName} — ${name}`;
    const img = $("#lightbox-img");
    img.src = isFace ? faceUrl(lb.groupId, current) : sourceUrl(lb.groupId, current);
    img.alt = name;
    img.title = current;

    const shown = lb.items.length;
    $("#lightbox-counter").textContent =
        shown > 1 ? `${lb.index + 1} of ${shown} shown · ${lb.total} total` : `${lb.total} total`;

    const prev = $("#lightbox-prev");
    const next = $("#lightbox-next");
    const showNav = shown > 1;
    prev.style.display = showNav ? "" : "none";
    next.style.display = showNav ? "" : "none";

    $("#lightbox-move").classList.toggle("hidden", !isFace);
    $("#lightbox-delete").classList.toggle("hidden", !isFace);

    const box = $("#lightbox-sources");
    box.innerHTML = "";
    const meta = document.createElement("div");
    meta.className = "meta-bar";
    meta.innerHTML =
        `<div class="meta-text"><div class="meta-name">${escapeHtml(name)}</div>` +
        `<div class="meta-path" title="${escapeHtml(current)}">${escapeHtml(isFace ? `Face crop · group ${lb.groupId}` : current)}</div></div>`;
    const copyBtn = document.createElement("button");
    copyBtn.className = "btn";
    copyBtn.textContent = "⧉ Copy path";
    copyBtn.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(current); toastSuccess("Path copied."); }
        catch (_) { toastSuccess(current); }
    });
    meta.appendChild(copyBtn);
    if (!isFace) {
        const revealBtn = document.createElement("button");
        revealBtn.className = "btn";
        revealBtn.textContent = "🗁 Reveal";
        revealBtn.addEventListener("click", async () => {
            const res = await revealInExplorer(lb.groupId, current);
            if (res) toastSuccess("Opened in Explorer.");
        });
        meta.appendChild(revealBtn);
    }
    box.appendChild(meta);
    if (!isFace) {
        const hint = document.createElement("div");
        hint.className = "muted small";
        hint.textContent = "Tip: use ← → to step through this page. Refine search or change pages in the detail panel for more.";
        box.appendChild(hint);
        loadFaceTags(lb, current, box);
    }
}

async function fetchTags(groupId, srcPath) {
    try {
        const data = await api(
            `/api/photo-tags?group_id=${groupId}&path=${encodeURIComponent(srcPath)}`
        );
        return data.tags || [];
    } catch (_) {
        return [];
    }
}

function loadFaceTags(lb, srcPath, box) {
    // Clear any overlay from the previous photo immediately.
    document.querySelector("#lightbox-imgwrap .tag-layer")?.remove();
    fetchTags(lb.groupId, srcPath).then((tags) => {
        // Guard on identity AND photo: navigation mutates the same object,
        // so a slow earlier fetch must not paint over a newer photo.
        if (state.lightbox !== lb || lb.items[lb.index] !== srcPath) return;
        if (tags.length === 0) {
            const note = document.createElement("div");
            note.className = "muted small";
            note.textContent =
                "No face tags on this photo yet — it was processed before tagging. Re-run grouping to add them.";
            box.appendChild(note);
            return;
        }
        const wrap = document.querySelector("#lightbox-imgwrap");
        if (!wrap) return;
        const layer = document.createElement("div");
        layer.className = "tag-layer";
        for (const t of tags) {
            const [x1, y1, x2, y2] = t.bbox;
            const el = document.createElement("div");
            el.className = "face-tag-box";
            el.style.left = `${x1 * 100}%`;
            el.style.top = `${y1 * 100}%`;
            el.style.width = `${Math.max(0, (x2 - x1)) * 100}%`;
            el.style.height = `${Math.max(0, (y2 - y1)) * 100}%`;
            el.title = `${t.name} — click to open this person`;
            el.innerHTML = `<span class="face-tag-name">${escapeHtml(t.name)}</span>`;
            el.addEventListener("click", (e) => {
                e.stopPropagation();
                document.dispatchEvent(
                    new CustomEvent("select-person", { detail: { groupId: t.group_id } })
                );
            });
            layer.appendChild(el);
        }
        wrap.appendChild(layer);
        const count = document.createElement("div");
        count.className = "muted small";
        count.textContent =
            `${tags.length} tagged face${tags.length === 1 ? "" : "s"} — hover a box for the name, click it to open that person.`;
        box.appendChild(count);
    });
}
