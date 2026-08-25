/* Server API operations for faces and undo */

import { api } from "./core.js";
import { toastError } from "./toast.js";

export async function deleteFaces(items) {
    try {
        return await api("/api/faces/bulk-delete", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ items }),
        });
    } catch (err) {
        toastError(err.message);
        return null;
    }
}

export async function moveFaces(items, targetGroupId, newGroupName = "") {
    try {
        return await api("/api/faces/bulk-move", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                items,
                target_group_id: targetGroupId,
                new_group_name: newGroupName,
            }),
        });
    } catch (err) {
        toastError(err.message);
        return null;
    }
}

export async function undoLast() {
    try {
        return await api("/api/undo", { method: "POST" });
    } catch (err) {
        toastError(err.message);
        return null;
    }
}

export async function renameGroup(groupId, name) {
    try {
        return await api(`/api/groups/${groupId}/rename`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name }),
        });
    } catch (err) {
        toastError(err.message);
        return null;
    }
}
