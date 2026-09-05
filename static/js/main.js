/* App entry: wiring, shortcuts, theme, config summary */

import { $, api, getDbFile, loadSettings, saveSettings, state } from "./core.js";
import { toastSuccess } from "./toast.js";
import { initBrowse } from "./browse.js";
import { initRun } from "./run.js";
import { initGroups, loadGroups } from "./groups.js";
import { closeLightbox, initLightbox, lightboxIsOpen, lightboxNavigate, openFaceViewer } from "./lightbox.js";
import { closeMoveModal, initMoveModal, moveModalIsOpen, openMoveModal } from "./movemodal.js";
import { undoLast } from "./ops.js";

function updateConfigSummary() {
    const folders = $("#input-folders").value.split("\n").map((l) => l.trim()).filter(Boolean).length;
    const db = getDbFile();
    const dbShort = db.length > 28 ? "…" + db.slice(-27) : db;
    $("#config-summary").textContent =
        `${folders} folder${folders === 1 ? "" : "s"} · ${dbShort} · threshold ${parseFloat($("#threshold").value).toFixed(2)}`;
}

$("#config-toggle").addEventListener("click", () => {
    const panel = $("#config-panel");
    panel.classList.toggle("collapsed");
    $("#config-toggle").setAttribute("aria-expanded", panel.classList.contains("collapsed") ? "false" : "true");
    saveSettings();
    updateConfigSummary();
});

$("#theme-btn").addEventListener("click", () => {
    const root = document.documentElement;
    root.dataset.theme = root.dataset.theme === "light" ? "dark" : "light";
    $("#theme-btn").textContent = root.dataset.theme === "dark" ? "☾" : "☀";
    saveSettings();
});

for (const id of ["input-folders", "output-faces", "db-file", "output-file"]) {
    $(`#${id}`).addEventListener("change", () => { saveSettings(); updateConfigSummary(); });
}
$("#threshold").addEventListener("input", () => {
    $("#threshold-value").textContent = parseFloat($("#threshold").value).toFixed(2);
    updateConfigSummary();
});
$("#threshold").addEventListener("change", saveSettings);

$("#refresh-btn").addEventListener("click", loadGroups);
$("#export-csv-btn").addEventListener("click", () => {
    document.querySelector("details.menu").removeAttribute("open");
    window.open(`/api/export?format=csv&db_file=${encodeURIComponent(getDbFile())}`, "_blank");
});
$("#export-json-btn").addEventListener("click", () => {
    document.querySelector("details.menu").removeAttribute("open");
    window.open(`/api/export?format=json&db_file=${encodeURIComponent(getDbFile())}`, "_blank");
});
$("#undo-btn").addEventListener("click", async () => {
    const result = await undoLast();
    if (!result) return;
    toastSuccess(`Undid ${result.action}: ${(result.details || []).join(", ")}`);
    loadGroups();
});

/* ---------- Keyboard shortcuts ---------- */

document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeLightbox(); closeMoveModal(); return; }
    const tag = document.activeElement?.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;

    if (lightboxIsOpen()) {
        switch (e.key) {
            case "ArrowRight": e.preventDefault(); lightboxNavigate(1); break;
            case "ArrowLeft": e.preventDefault(); lightboxNavigate(-1); break;
            case "m": case "M": $("#lightbox-move").click(); break;
            case "Delete": case "Backspace": e.preventDefault(); $("#lightbox-delete").click(); break;
        }
        return;
    }
    if (moveModalIsOpen()) return;

    const tiles = [...document.querySelectorAll("#person-detail .face-tile")];
    const focusedIdx = tiles.indexOf(document.activeElement);
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        e.preventDefault();
        if (!tiles.length) return;
        let next;
        if (focusedIdx === -1) next = e.key === "ArrowRight" ? 0 : tiles.length - 1;
        else next = (focusedIdx + (e.key === "ArrowRight" ? 1 : -1) + tiles.length) % tiles.length;
        focusTile(tiles[next]);
    } else if (focusedIdx !== -1 && (e.key === "Enter" || e.key === "m" || e.key === "M" || e.key === "Delete")) {
        e.preventDefault();
        const tile = tiles[focusedIdx];
        const group = state.groups.find((g) => g.id == tile.dataset.groupId);
        if (!group) return;
        const filename = tile.dataset.filename;
        if (e.key === "Enter") {
            openFaceViewer(group, state.faces.items, state.faces.items.indexOf(filename), state.faces.total);
        } else if (e.key.toLowerCase() === "m") {
            tile.dispatchEvent(new CustomEvent("request-move", { bubbles: true }));
        } else {
            tile.querySelector(".delete-btn").click();
        }
    }
});

function focusTile(tile) {
    document.querySelectorAll(".face-tile.focused").forEach((t) => t.classList.remove("focused"));
    tile.classList.add("focused");
    tile.focus();
    tile.scrollIntoView({ block: "nearest" });
}

document.addEventListener("request-move", (e) => {
    const tile = e.target.closest ? e.target.closest(".face-tile") : e.target;
    if (!tile?.dataset) return;
    const group = state.groups.find((g) => g.id == tile.dataset.groupId);
    if (group) openMoveModal(group, tile.dataset.filename);
});

/* ---------- Init ---------- */

initBrowse();
initRun(loadGroups);
initGroups(loadGroups);
initLightbox(loadGroups);
initMoveModal(loadGroups);

loadSettings();
updateConfigSummary();
loadGroups();
