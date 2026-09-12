/* Top-level pages: People | Review. Hash-routed (#/people, #/review) with a
   localStorage fallback; the Review tab carries a badge with the number of
   open items (duplicate sets + faceless photos) so nothing gets ignored. */

import { getDupeSetCount } from "./duplicates.js";
import { getFacelessCount } from "./faceless.js";

const PAGE_KEY = "facedeck-page";

function pageFromHash() {
    const h = (window.location.hash || "").replace(/^#\/?/, "");
    return h === "review" ? "review" : null;
}

function savedPage() {
    try {
        return localStorage.getItem(PAGE_KEY) === "review" ? "review" : null;
    } catch (_) {
        return null;
    }
}

export function currentPage() {
    return document.querySelector("#page-review")?.classList.contains("hidden")
        ? "people"
        : "review";
}

export function showPage(name, push = false) {
    if (name !== "review") name = "people";
    for (const p of document.querySelectorAll(".page")) {
        p.classList.toggle("hidden", p.id !== `page-${name}`);
    }
    for (const b of document.querySelectorAll("[data-page]")) {
        b.classList.toggle("active", b.dataset.page === name);
        if (b.dataset.page === name) b.setAttribute("aria-current", "page");
        else b.removeAttribute("aria-current");
    }
    try {
        localStorage.setItem(PAGE_KEY, name);
    } catch (_) {}
    const want = `#/${name}`;
    if (push && window.location.hash !== want) window.location.hash = want;
    updateNavBadges();
}

export function updateNavBadges() {
    const badge = document.querySelector("#nav-review-badge");
    if (!badge) return;
    const n = (getDupeSetCount() || 0) + (getFacelessCount() || 0);
    badge.textContent = n > 99 ? "99+" : String(n);
    badge.hidden = n === 0;
}

export function initNav() {
    for (const b of document.querySelectorAll("[data-page]")) {
        b.addEventListener("click", () => showPage(b.dataset.page, true));
    }
    window.addEventListener("hashchange", () => showPage(pageFromHash() || "people", false));
    document.addEventListener("counts-changed", updateNavBadges);
    showPage(pageFromHash() || savedPage() || "people", false);
}
