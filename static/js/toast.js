/* Toast notifications */

let wrap = null;

function ensureWrap() {
    if (!wrap) {
        wrap = document.createElement("div");
        wrap.className = "toast-wrap";
        document.body.appendChild(wrap);
    }
    return wrap;
}

export function toast(message, type = "info", timeoutMs = 3500) {
    const el = document.createElement("div");
    el.className = `toast toast-${type}`;
    el.textContent = message;
    ensureWrap().appendChild(el);
    requestAnimationFrame(() => el.classList.add("visible"));
    setTimeout(() => {
        el.classList.remove("visible");
        setTimeout(() => el.remove(), 300);
    }, timeoutMs);
}

export const toastError = (msg) => toast(msg, "error");
export const toastSuccess = (msg) => toast(msg, "success");

/* Suffix for move toasts: how many source photos followed their faces. */
export function photosSuffix(result) {
    const n = new Set((result.photos || []).map((p) => p.photo)).size;
    return n ? ` ${n} photo(s) linked here too.` : "";
}
