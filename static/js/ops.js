/* Server API operations */

import { api, getDbFile } from "./core.js";
import { toastError } from "./toast.js";

export async function deleteFaces(items) {
    try {
        return await api("/api/faces/bulk-delete", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ items }),
        });
    } catch (err) { toastError(err.message); return null; }
}

export async function moveFaces(items, targetGroupId, newGroupName = "") {
    try {
        return await api("/api/faces/bulk-move", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ items, target_group_id: targetGroupId, new_group_name: newGroupName }),
        });
    } catch (err) { toastError(err.message); return null; }
}

export async function undoLast() {
    try {
        return await api("/api/undo", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ db_file: getDbFile() }),
        });
    } catch (err) { toastError(err.message); return null; }
}

export async function renameGroup(groupId, name) {
    try {
        return await api(`/api/groups/${groupId}/rename`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, db_file: getDbFile() }),
        });
    } catch (err) { toastError(err.message); return null; }
}

export async function approveGroup(groupId) {
    try {
        return await api(`/api/groups/${groupId}/approve`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ db_file: getDbFile() }),
        });
    } catch (err) { toastError(err.message); return null; }
}

export async function approveAllGroups() {
    try {
        return await api("/api/groups/approve-all", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ db_file: getDbFile() }),
        });
    } catch (err) { toastError(err.message); return null; }
}

export async function revealInExplorer(groupId, path) {
    try {
        return await api("/api/reveal", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ group_id: groupId, path, db_file: getDbFile() }),
        });
    } catch (err) { toastError(err.message); return null; }
}

export async function revealGroupFolder(groupId) {
    try {
        return await api(`/api/groups/${groupId}/reveal-folder`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ db_file: getDbFile() }),
        });
    } catch (err) { toastError(err.message); return null; }
}
