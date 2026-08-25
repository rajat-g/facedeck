const $ = (sel) => document.querySelector(sel);

const state = {
    groups: [],
    moveContext: null, // {groupId, filename}
    pollTimer: null,
};

function getDbFile() {
    return $("#db-file").value.trim() || "processing_state.db";
}

async function api(path, options = {}) {
    const url = new URL(path, window.location.origin);
    if (!["POST", "DELETE"].includes(options.method || "GET") && !path.includes("db_file")) {
        url.searchParams.set("db_file", getDbFile());
    }
    const res = await fetch(url, options);
    let body = {};
    try {
        body = await res.json();
    } catch (_) {
        /* empty body */
    }
    if (!res.ok) {
        throw new Error(body.error || `Request failed (${res.status})`);
    }
    return body;
}

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
}

/* ---------- Running ---------- */

async function startRun() {
    const payload = {
        input_folder: $("#input-folder").value.trim(),
        output_faces: $("#output-faces").value.trim(),
        db_file: getDbFile(),
        output_file: $("#output-file").value.trim(),
        threshold: parseFloat($("#threshold").value),
    };

    try {
        await api("/api/run", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
    } catch (err) {
        alert(err.message);
        return;
    }

    $("#run-btn").disabled = true;
    $("#status-panel").classList.remove("hidden");
    state.pollTimer = setInterval(pollStatus, 1000);
}

async function pollStatus() {
    let status;
    try {
        status = await api("/api/status");
    } catch (_) {
        return;
    }

    const pct = status.total > 0 ? (status.processed / status.total) * 100 : 0;
    $("#progress-bar").style.width = `${pct}%`;
    $("#progress-text").textContent =
        status.total > 0 ? `${status.processed} / ${status.total}` : "Working...";
    $("#log-output").textContent = status.log.join("\n");
    $("#log-output").scrollTop = $("#log-output").scrollHeight;

    const errorBox = $("#error-box");
    if (status.error) {
        errorBox.textContent = status.error;
        errorBox.classList.remove("hidden");
    } else {
        errorBox.classList.add("hidden");
    }

    if (!status.running) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        $("#run-btn").disabled = false;
        loadGroups();
    }
}

/* ---------- Groups ---------- */

async function loadGroups() {
    let data;
    try {
        data = await api("/api/groups");
    } catch (err) {
        alert(`Failed to load groups: ${err.message}`);
        return;
    }
    state.groups = data.groups;
    renderGroups();
}

function renderGroups() {
    const container = $("#groups-container");
    container.innerHTML = "";
    $("#groups-empty").classList.toggle("hidden", state.groups.length > 0);

    for (const group of state.groups) {
        const card = document.createElement("div");
        card.className = "group-card";

        const header = document.createElement("div");
        header.className = "group-header";
        header.innerHTML = `
            <input type="text" class="group-name" value="${escapeHtml(group.name)}">
            <span class="face-count">${group.faces.length} face(s)</span>
            <button class="rename-btn">Rename</button>
        `;
        card.appendChild(header);

        const renameBtn = header.querySelector(".rename-btn");
        const nameInput = header.querySelector(".group-name");
        renameBtn.addEventListener("click", async () => {
            const newName = nameInput.value.trim();
            if (!newName || newName === group.name) return;
            try {
                await api(`/api/groups/${group.id}/rename`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ name: newName, db_file: getDbFile() }),
                });
                group.name = newName;
            } catch (err) {
                alert(err.message);
                nameInput.value = group.name;
            }
        });
        nameInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter") renameBtn.click();
        });

        const grid = document.createElement("div");
        grid.className = "faces-grid";

        if (group.faces.length === 0) {
            grid.innerHTML = '<p class="empty-group">No cropped faces in this group.</p>';
        }

        for (const filename of group.faces) {
            grid.appendChild(renderFaceTile(group, filename));
        }

        card.appendChild(grid);
        container.appendChild(card);
    }
}

function renderFaceTile(group, filename) {
    const tile = document.createElement("div");
    tile.className = "face-tile";
    tile.innerHTML = `
        <img src="/api/groups/${group.id}/faces/${encodeURIComponent(filename)}?db_file=${encodeURIComponent(getDbFile())}"
             alt="${escapeHtml(filename)}" loading="lazy">
        <div class="face-name" title="${escapeHtml(filename)}">${escapeHtml(filename)}</div>
        <div class="tile-actions">
            <button class="move-btn" title="Move to another group">&#8646;</button>
            <button class="delete-btn" title="Remove this face">&#10005;</button>
        </div>
    `;

    tile.querySelector(".delete-btn").addEventListener("click", async () => {
        if (!confirm(`Remove "${filename}"?\nThe image file will be deleted.`)) return;
        try {
            await api(
                `/api/groups/${group.id}/faces/${encodeURIComponent(filename)}?db_file=${encodeURIComponent(getDbFile())}`,
                { method: "DELETE" },
            );
            loadGroups();
        } catch (err) {
            alert(err.message);
        }
    });

    tile.querySelector(".move-btn").addEventListener("click", () => openMoveModal(group, filename));

    return tile;
}

/* ---------- Move modal ---------- */

function openMoveModal(group, filename) {
    state.moveContext = { groupId: group.id, filename };

    $("#move-preview").src = `/api/groups/${group.id}/faces/${encodeURIComponent(filename)}?db_file=${encodeURIComponent(getDbFile())}`;

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

function closeMoveModal() {
    $("#move-modal").classList.add("hidden");
    state.moveContext = null;
}

async function confirmMove() {
    if (!state.moveContext) return;

    const useExisting = document.querySelector('input[name="move-target"]:checked').value === "existing";
    const payload = {
        group_id: state.moveContext.groupId,
        filename: state.moveContext.filename,
        db_file: getDbFile(),
    };

    if (useExisting) {
        const select = $("#move-target-select");
        if (select.disabled || !select.value) {
            alert("There is no other group to move this face to.");
            return;
        }
        payload.target_group_id = parseInt(select.value, 10);
    } else {
        payload.target_group_id = -1;
        payload.new_group_name = $("#move-new-name").value.trim();
    }

    try {
        await api("/api/faces/move", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        closeMoveModal();
        loadGroups();
    } catch (err) {
        alert(err.message);
    }
}

/* ---------- Init ---------- */

$("#threshold").addEventListener("input", () => {
    $("#threshold-value").textContent = parseFloat($("#threshold").value).toFixed(2);
});

$("#run-btn").addEventListener("click", startRun);
$("#refresh-btn").addEventListener("click", loadGroups);
$("#move-cancel").addEventListener("click", closeMoveModal);
$("#move-confirm").addEventListener("click", confirmMove);
$("#move-modal").addEventListener("click", (e) => {
    if (e.target === e.currentTarget) closeMoveModal();
});

loadGroups();
