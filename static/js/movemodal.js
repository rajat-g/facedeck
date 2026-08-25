/* Move-face modal (single face) */

import { $, state } from "./core.js";
import { moveFaces } from "./ops.js";
import { toastError, toastSuccess } from "./toast.js";

let refresh = () => {};

export function initMoveModal(loadGroups) {
    refresh = loadGroups;

    $("#move-cancel").addEventListener("click", closeMoveModal);
    $("#move-confirm").addEventListener("click", confirmMove);
    $("#move-modal").addEventListener("click", (e) => {
        if (e.target === e.currentTarget) closeMoveModal();
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
        opt.textContent = `${g.name} (${g.faces.length} faces)`;
        select.appendChild(opt);
    }
    select.disabled = state.groups.length <= 1;

    const newRadio = document.querySelector('input[name="move-target"][value="new"]');
    if (state.groups.length <= 1) {
        newRadio.checked = true;
        document.querySelector('input[name="move-target"][value="existing"]').disabled = true;
    } else {
        document.querySelector('input[name="move-target"][value="existing"]').disabled = false;
    }
    $("#move-new-name").value = "";

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

    const useExisting =
        document.querySelector('input[name="move-target"]:checked').value === "existing";
    const select = $("#move-target-select");

    if (useExisting && (select.disabled || !select.value)) {
        toastError("There is no other group to move this face to.");
        return;
    }

    const result = await moveFaces(
        [state.moveContext],
        useExisting ? parseInt(select.value, 10) : -1,
        $("#move-new-name").value.trim(),
    );
    if (!result) return;

    closeMoveModal();
    toastSuccess(`Moved ${result.moved} face(s).`);
    refresh();
}
