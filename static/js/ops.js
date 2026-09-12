/* Server API operations.
   Mutations are blocked while a grouping run is in progress: the runner
   checkpoints its in-memory state over the DB, which would clobber
   concurrent curation. Renames are exempt (checkpoint preserves them). */

import { api, getDbFile, isRunning } from "./core.js";
import { toastError } from "./toast.js";

function blockedByRun() {
    if (isRunning()) {
        toastError("A grouping run is in progress — curation unlocks when it finishes.");
        return true;
    }
    return false;
}

export async function deleteFaces(items) {
    if (blockedByRun()) return null;
    try {
        return await api("/api/faces/bulk-delete", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ db_file: getDbFile(), items }),
        });
    } catch (err) { toastError(err.message); return null; }
}

export async function moveFaces(items, targetGroupId, newGroupName = "") {
    if (blockedByRun()) return null;
    try {
        return await api("/api/faces/bulk-move", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ db_file: getDbFile(), items, target_group_id: targetGroupId, new_group_name: newGroupName }),
        });
    } catch (err) { toastError(err.message); return null; }
}

export async function undoLast() {
    if (blockedByRun()) return null;
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
    if (blockedByRun()) return null;
    try {
        return await api(`/api/groups/${groupId}/approve`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ db_file: getDbFile() }),
        });
    } catch (err) { toastError(err.message); return null; }
}

export async function approveAllGroups() {
    if (blockedByRun()) return null;
    try {
        return await api(`/api/groups/approve-all`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ db_file: getDbFile() }),
        });
    } catch (err) { toastError(err.message); return null; }
}

export async function deleteGroup(groupId) {
    if (blockedByRun()) return null;
    try {
        return await api(
            `/api/groups/${groupId}?db_file=${encodeURIComponent(getDbFile())}`,
            { method: "DELETE" },
        );
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
