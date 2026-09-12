/* Photos with no detected faces: processed, grouped nowhere, lost nowhere. */

import { $, api, basename, escapeHtml, getDbFile, shortDir } from "./core.js";
import { toastError, toastSuccess } from "./toast.js";
import { openFacelessViewer } from "./lightbox.js";
import { revealInExplorer } from "./ops.js";

export function initFaceless(onRescan) {
    $("#faceless-refresh").addEventListener("click", loadFaceless);
    $("#faceless-rescan").addEventListener("click", () => {
        if (!getFacelessCount()) {
            toastError("No faceless photos to rescan.");
            return;
        }
        const det = parseFloat($("#faceless-det").value) || 0.3;
        if (!confirm(
            `Rescan ${getFacelessCount()} photo(s) with no detected faces at detection threshold ${det}?\n\n` +
            `Only these photos are re-processed — everything else is skipped. ` +
            `Lower thresholds find more faces but also more false alarms.`
        )) return;
        onRescan(det);
    });
}

let facelessTotal = 0;

export function getFacelessCount() {
    return facelessTotal;
}

export async function loadFaceless() {
    let photos = [];
    try {
        const data = await api("/api/faceless");
        photos = data.photos || [];
    } catch (err) {
        toastError(`Failed to load faceless photos: ${err.message}`);
        return;
    }
    renderFaceless(photos);
}

function renderFaceless(photos) {
    facelessTotal = photos.length;
    document.dispatchEvent(new CustomEvent("counts-changed"));
    $("#faceless-count").textContent = photos.length
        ? `— ${photos.length} photo${photos.length === 1 ? "" : "s"}`
        : "";
    const box = $("#faceless-list");
    box.innerHTML = "";
    if (!photos.length) {
        box.innerHTML =
            `<p class="muted small dupe-empty">Every processed photo has at least one detected face.</p>`;
        return;
    }
    const rows = document.createElement("div");
    rows.className = "photo-rows";
    const paths = photos.map((p) => p.path);
    photos.forEach((p, i) => rows.appendChild(facelessRow(paths, p, i)));
    box.appendChild(rows);
}

function facelessRow(paths, p, i) {
    const row = document.createElement("div");
    row.className = "photo-row lines";
    row.title = p.path;
    const scanned = (p.last_det === null || p.last_det === undefined)
        ? "" : ` · last scan ${p.last_det}`;
    const nrej = Number(p.rejected) || 0;
    row.innerHTML =
        `<span class="photo-meta"><span class="photo-name">${escapeHtml(basename(p.path))}</span>` +
        `<span class="photo-sub muted small">${escapeHtml(shortDir(p.path))}` +
        `${p.exists ? "" : " · file missing"}${escapeHtml(scanned)}` +
        `${nrej ? ` · ${nrej} rejected` : ""}</span></span>` +
        `<span class="photo-actions"><button class="btn preview">Preview</button>` +
        (nrej ? `<button class="btn allow" title="Allow these faces again — the next rescan may find them anew">Allow again</button>` : "") +
        `<button class="btn reveal">Reveal</button></span>`;
    row.querySelector(".preview").addEventListener("click", () => {
        openFacelessViewer(paths, i, paths.length);
    });
    const allowBtn = row.querySelector(".allow");
    if (allowBtn) allowBtn.addEventListener("click", async () => {
        try {
            await api("/api/rejected/clear", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ db_file: getDbFile(), paths: [p.path] }),
            });
        } catch (err) {
            toastError(err.message);
            return;
        }
        toastSuccess(`“${basename(p.path)}” will be reconsidered on the next rescan.`);
        loadFaceless();
    });
    row.querySelector(".reveal").addEventListener("click", async () => {
        const res = await revealInExplorer(null, p.path);
        if (res) toastSuccess("Opened in Explorer.");
    });
    return row;
}
