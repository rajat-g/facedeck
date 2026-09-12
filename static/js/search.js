/* Search by example: upload a photo, rank matching people per detected face.
   Read-only — nothing is stored, grouped or trashed. */

import { $, escapeHtml, getDbFile } from "./core.js";
import { toastError } from "./toast.js";
import { showPage } from "./nav.js";

export function initSearch() {
    const input = $("#search-file");
    $("#search-choose").addEventListener("click", () => input.click());
    input.addEventListener("change", () => {
        if (input.files[0]) runSearch(input.files[0]);
        input.value = "";
    });
    const dz = $("#search-drop");
    for (const ev of ["dragover", "dragenter"]) {
        dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); });
    }
    for (const ev of ["dragleave", "drop"]) {
        dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); });
    }
    dz.addEventListener("drop", (e) => {
        const f = e.dataTransfer.files && e.dataTransfer.files[0];
        if (f) runSearch(f);
    });
    if (!navigator.mediaDevices?.getUserMedia) {
        $("#search-use-camera").classList.add("hidden");
    } else {
        $("#search-use-camera").addEventListener("click", startCamera);
    }
    $("#camera-cancel").addEventListener("click", stopCamera);
    $("#camera-capture").addEventListener("click", captureAndSearch);
    $("#camera-select").addEventListener("change", (e) => switchCamera(e.target.value));
    // Never leave the camera running behind another tab.
    window.addEventListener("hashchange", () => {
        if ((window.location.hash || "").replace(/^#\/?/, "") !== "search") stopCamera();
    });
}

/* ---------- Camera capture (frames go through the same upload search) ---------- */

let camStream = null;

function stopCameraTracks() {
    if (camStream) camStream.getTracks().forEach((t) => t.stop());
    camStream = null;
}

export function stopCamera() {
    stopCameraTracks();
    const video = $("#camera-video");
    if (video) video.srcObject = null;
    $("#camera-box")?.classList.add("hidden");
}

function cameraError(err) {
    const name = err && err.name;
    if (name === "NotAllowedError" || name === "SecurityError") {
        return "Camera permission denied — allow access in the browser, then try again.";
    }
    if (name === "NotFoundError" || name === "OverconstrainedError") {
        return "No camera found on this device.";
    }
    if (!window.isSecureContext) {
        return "Camera needs a secure page — open the app via localhost or HTTPS.";
    }
    return `Could not start the camera (${name || "unknown error"}).`;
}

async function startCamera() {
    stopCameraTracks();
    try {
        camStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
    } catch (err) {
        toastError(cameraError(err));
        return;
    }
    try {
        const cams = (await navigator.mediaDevices.enumerateDevices())
            .filter((d) => d.kind === "videoinput");
        const sel = $("#camera-select");
        sel.innerHTML = "";
        cams.forEach((c, i) => {
            const o = document.createElement("option");
            o.value = c.deviceId;
            o.textContent = c.label || `Camera ${i + 1}`;
            sel.appendChild(o);
        });
        sel.classList.toggle("hidden", cams.length < 2);
    } catch (_) { /* labels need permission; stream already covers preview */ }
    $("#camera-video").srcObject = camStream;
    $("#camera-box").classList.remove("hidden");
    $("#camera-box").scrollIntoView({ block: "nearest" });
}

async function switchCamera(deviceId) {
    if (!deviceId) return;
    stopCameraTracks();
    try {
        camStream = await navigator.mediaDevices.getUserMedia({
            video: { deviceId: { exact: deviceId } }, audio: false,
        });
        $("#camera-video").srcObject = camStream;
    } catch (err) {
        toastError(cameraError(err));
        stopCamera();
    }
}

async function captureAndSearch() {
    const video = $("#camera-video");
    if (!camStream || !video.videoWidth) {
        toastError("Camera is not ready yet — wait a moment and retry.");
        return;
    }
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d").drawImage(video, 0, 0);
    const blob = await new Promise((res) => canvas.toBlob(res, "image/jpeg", 0.92));
    stopCamera();
    if (!blob) {
        toastError("Capture failed — try again.");
        return;
    }
    runSearch(new File([blob], "camera.jpg", { type: "image/jpeg" }));
}

async function runSearch(file) {
    const box = $("#search-results");
    box.innerHTML = `<p class="muted">Detecting faces…</p>`;
    const form = new FormData();
    form.append("image", file);
    form.append("db_file", getDbFile());
    form.append("threshold", document.querySelector("#threshold").value);
    let data;
    try {
        const res = await fetch("/api/search", { method: "POST", body: form });
        try {
            data = await res.json();
        } catch (_) {
            data = {};
        }
        if (!res.ok) throw new Error(data.error || `Search failed (${res.status})`);
    } catch (err) {
        box.innerHTML = "";
        toastError(err.message);
        return;
    }
    renderResults(box, data);
}

function renderResults(box, data) {
    box.innerHTML = "";
    if (!data.faces || !data.faces.length) {
        box.innerHTML = `<p class="muted">No faces found.</p>`;
        return;
    }
    if (data.image) {
        const wrap = document.createElement("div");
        wrap.className = "search-query";
        const img = document.createElement("img");
        img.src = data.image;
        img.alt = "Uploaded photo";
        wrap.appendChild(img);
        const layer = document.createElement("div");
        layer.className = "tag-layer";
        data.faces.forEach((f, i) => {
            if (!f.bbox) return;
            const [x1, y1, x2, y2] = f.bbox;
            const el = document.createElement("div");
            el.className = "face-tag-box search-num";
            el.style.left = `${x1 * 100}%`;
            el.style.top = `${y1 * 100}%`;
            el.style.width = `${Math.max(0, x2 - x1) * 100}%`;
            el.style.height = `${Math.max(0, y2 - y1) * 100}%`;
            el.innerHTML = `<span class="face-tag-name">${i + 1}</span>`;
            layer.appendChild(el);
        });
        wrap.appendChild(layer);
        box.appendChild(wrap);
    }
    data.faces.forEach((f, i) => box.appendChild(renderFaceResult(f, i)));
}

function renderFaceResult(f, i) {
    const div = document.createElement("div");
    div.className = "search-face";
    const head = document.createElement("div");
    head.className = "search-face-head";
    head.innerHTML = (f.thumb ? `<img src="${f.thumb}" alt="Face ${i + 1}">` : "") +
        `<strong>Face ${i + 1}</strong>`;
    div.appendChild(head);
    if (!f.matches || !f.matches.length) {
        const p = document.createElement("p");
        p.className = "muted small";
        p.style.padding = "10px 12px";
        p.textContent = "No people to compare against yet — run grouping first.";
        div.appendChild(p);
        return div;
    }
    const list = document.createElement("div");
    list.className = "search-matches";
    for (const m of f.matches) {
        const b = document.createElement("button");
        b.className = "search-match";
        b.innerHTML = `<span class="person-name">${escapeHtml(m.name)}</span>` +
            `<span class="mono muted small">${Number(m.score).toFixed(2)}</span>` +
            (m.join ? `<span class="badge approved">would join</span>`
                    : `<span class="badge">below threshold</span>`);
        b.title = `Open ${m.name} (similarity ${m.score})`;
        b.addEventListener("click", () => {
            document.dispatchEvent(
                new CustomEvent("select-person", { detail: { groupId: m.group_id } }));
            showPage("people", true);
        });
        list.appendChild(b);
    }
    div.appendChild(list);
    return div;
}
