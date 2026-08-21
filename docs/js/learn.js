(() => {
    "use strict";

    const activePanel = () => document.querySelector("[data-page-language]:not([hidden])");
    let activeTag = "all";
    let searchQuery = "";

    function language() {
        return document.documentElement.lang?.toLowerCase().startsWith("zh") ? "zh" : "en";
    }

    function applyFilters() {
        const panel = activePanel();
        if (!panel) return;
        const query = searchQuery.trim().toLocaleLowerCase();
        let visible = 0;
        for (const entry of panel.querySelectorAll("[data-learn-entry]")) {
            const tags = (entry.dataset.tags || "").split(/\s+/).filter(Boolean);
            const searchable = entry.dataset[language() === "zh" ? "searchZh" : "searchEn"] || "";
            const matchesTag = activeTag === "all" || tags.includes(activeTag);
            const matchesQuery = !query || searchable.toLocaleLowerCase().includes(query);
            entry.hidden = !(matchesTag && matchesQuery);
            if (!entry.hidden) visible += 1;
        }
        const status = panel.querySelector("[data-learn-status]");
        if (status) status.textContent = language() === "zh" ? `找到 ${visible} 个章节` : `${visible} chapter${visible === 1 ? "" : "s"}`;
    }

    document.addEventListener("DOMContentLoaded", () => {
        document.querySelectorAll("[data-learn-search]").forEach((input) => {
            const updateSearch = () => {
                searchQuery = input.value || "";
                document.querySelectorAll("[data-learn-search]").forEach((candidate) => {
                    if (candidate !== input) candidate.value = searchQuery;
                });
                applyFilters();
            };
            input.addEventListener("input", updateSearch);
            input.addEventListener("search", updateSearch);
        });
        document.querySelectorAll("[data-learn-tag]").forEach((button) => {
            button.addEventListener("click", () => {
                activeTag = button.dataset.learnTag || "all";
                document.querySelectorAll("[data-learn-tag]").forEach((candidate) => {
                    const selected = candidate.dataset.learnTag === activeTag;
                    candidate.classList.toggle("is-active", selected);
                    candidate.setAttribute("aria-pressed", String(selected));
                });
                applyFilters();
            });
        });
        document.addEventListener("site:language-changed", applyFilters);
        applyFilters();
    });
})();
