/* Move-face dialog (single face) */

import { $, faceUrl, state } from "./core.js";
import { moveFaces } from "./ops.js";
import { photosSuffix, toastError, toastSuccess } from "./toast.js";

let refresh = () => {};

export function initMoveModal(loadGroups) {
    refresh = loadGroups;
    $("#move-cancel").addEventListener("click", closeMoveModal);
    $("#move-confirm").addEventListener("click", confirmMove);
    $("#move-modal").addEventListener("click", (e) => {
        if (e.target === e.currentTarget) closeMoveModal();
    });
    // Lightbox delegates single-face moves here.
    document.addEventListener("request-move-explicit", (e) => {
        const { groupId, filename } = e.detail || {};
        const group = state.groups.find((g) => g.id === groupId);
        if (group && filename) openMoveModal(group, filename);
    });
}

export function openMoveModal(group, filename) {
    state.moveContext = { groupId: group.id, filename };
    const select = $("#move-target-select");
    select.innerHTML = "";
    for (const g of state.groups) {
        if (g.id === group.id) continue;
        const opt = document.createElement("option");
        opt.value = g.id;
        opt.textContent = `${g.name} (${g.face_count} faces)`;
        select.appendChild(opt);
    }
    select.disabled = state.groups.length <= 1;

    const existingRadio = document.querySelector('input[name="move-target"][value="existing"]');
    const newRadio = document.querySelector('input[name="move-target"][value="new"]');
    if (state.groups.length <= 1) {
        newRadio.checked = true;
        existingRadio.disabled = true;
    } else {
        existingRadio.disabled = false;
        existingRadio.checked = true;
    }
    $("#move-new-name").value = "";
    const preview = $("#move-preview");
    preview.src = faceUrl(group.id, filename);
    preview.alt = filename;
    $("#move-modal").classList.remove("hidden");
}

export function closeMoveModal() {
    $("#move-modal").classList.add("hidden");
    state.moveContext = null;
}

export function moveModalIsOpen() {
    return !$("#move-modal").classList.contains("hidden");
}

async function confirmMove() {
    if (!state.moveContext) return;
    const useExisting = document.querySelector('input[name="move-target"]:checked').value === "existing";
    const select = $("#move-target-select");
    if (useExisting && (select.disabled || !select.value)) {
        toastError("There is no other group to move this face to.");
        return;
    }
    const result = await moveFaces(
        [{ group_id: state.moveContext.groupId, filename: state.moveContext.filename }],
        useExisting ? parseInt(select.value, 10) : -1,
        $("#move-new-name").value.trim(),
    );
    if (!result) return;
    closeMoveModal();
    toastSuccess(`Moved ${result.moved} face(s).` + photosSuffix(result));
    refresh();
}
