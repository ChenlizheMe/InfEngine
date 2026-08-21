(function () {
    function normalized(value, locale = "en") {
        return String(value || "")
            .toLocaleLowerCase(locale === "zh-CN" ? "zh-CN" : "en")
            .replace(/\s+/g, " ")
            .trim();
    }

    function buildSearchModel(apiIndex, docsIndex) {
        const apiRelease = apiIndex?.generated_for_release;
        const docsRelease = docsIndex?.generated_for_release;
        if (!apiRelease && !docsRelease) {
            throw new Error("Documentation search requires at least one versioned index.");
        }
        if (apiRelease && docsRelease && apiRelease !== docsRelease) {
            throw new Error("Documentation search indexes describe different releases.");
        }
        const release = docsRelease || apiRelease;

        const api = (apiIndex?.symbols || []).map((item) => ({
            title: item.symbol,
            kind: item.kind,
            module: item.module,
            summary: item.summary || (item.signatures || []).join(" · "),
            searchable: [item.symbol, item.module, item.kind, item.summary, ...(item.signatures || [])].join(" "),
            status: item.status,
            language: item.language,
            layer: "api",
            url: item.url,
            priority: 20
        }));
        const docs = (docsIndex?.documents || []).map((item) => ({
            title: item.title,
            summary: item.summary,
            searchable: [item.title, item.layer, item.summary, item.status, item.since, ...(item.tags || []), ...(item.audience || [])].join(" "),
            status: item.status,
            language: item.language,
            layer: item.layer,
            url: item.url,
            priority: item.layer === "learn" ? 40 : item.layer === "manual" ? 35 : 25
        }));
        return {
            release,
            items: [...docs, ...api],
            sources: { api: Boolean(apiRelease), docs: Boolean(docsRelease) }
        };
    }

    function score(item, rawQuery, locale) {
        const query = normalized(rawQuery, locale);
        if (!query) return item.priority;
        const title = normalized(item.title, locale);
        const haystack = normalized(item.searchable, locale);
        const tokens = [...new Set(query.split(" ").filter(Boolean))];
        if (!tokens.every((token) => title.includes(token) || haystack.includes(token))) return -1;
        let value = item.priority;
        if (title === query) value += 300;
        else if (title.startsWith(query)) value += 190;
        else if (title.includes(query)) value += 120;
        for (const token of tokens) {
            if (title.startsWith(token)) value += 70;
            else if (title.includes(token)) value += 45;
            if (haystack.includes(token)) value += 10;
        }
        return value;
    }

    function search(model, options = {}) {
        const query = String(options.query || "").trim();
        const language = options.language || "all";
        const layer = options.layer || "all";
        const status = options.status || "all";
        const locale = options.locale || "en";
        const limit = Number.isInteger(options.limit) && options.limit > 0 ? options.limit : 16;
        const facetActive = layer !== "all" || status !== "all";
        if (!query && !facetActive) return { total: 0, matches: [] };

        const ranked = model.items
            .filter((item) => language === "all" || item.language === language)
            .filter((item) => layer === "all" || item.layer === layer)
            .filter((item) => status === "all" || item.status === status)
            .map((item) => ({ item, score: score(item, query, locale) }))
            .filter((entry) => entry.score >= 0)
            .sort((left, right) => right.score - left.score
                || left.item.title.localeCompare(right.item.title, locale === "zh-CN" ? "zh-CN" : "en"));
        return { total: ranked.length, matches: ranked.slice(0, limit) };
    }

    function resultNavigationIndex(currentIndex, key, itemCount) {
        if (!itemCount) return -1;
        if (key === "Home") return 0;
        if (key === "End") return itemCount - 1;
        if (key === "ArrowDown") return currentIndex < 0 || currentIndex >= itemCount - 1 ? 0 : currentIndex + 1;
        if (key === "ArrowUp") return currentIndex <= 0 ? itemCount - 1 : currentIndex - 1;
        return currentIndex;
    }

    function buildWikiSearchUrl(options = {}) {
        const params = new URLSearchParams();
        const query = String(options.query || "").trim();
        const layer = String(options.layer || "all");
        const status = String(options.status || "all");
        const language = options.language === "zh-CN" || options.language === "zh"
            ? "zh"
            : options.language === "en" ? "en" : "";
        if (query) params.set("q", query);
        if (layer !== "all") params.set("layer", layer);
        if (status !== "all") params.set("status", status);
        if (language) params.set("lang", language);
        const queryString = params.toString();
        const base = language === "zh" ? "/wiki/site/zh/api/index.html" : "/wiki/site/en/api/index.html";
        return queryString ? `${base}?${queryString}` : base;
    }

    const copyByLanguage = {
        en: {
            title: "Search Infernux documentation",
            placeholder: "API symbol, module, or method…",
            close: "Close documentation search",
            trigger: "Search documentation",
            triggerText: "Search docs",
            loading: "Loading the API index…",
            ready: "Search the selected language or filter by stability.",
            empty: "No API symbol matches this query and filter set.",
            failed: "API search is temporarily unavailable. Close and retry when the connection recovers.",
            apiUnavailable: "The API index is unavailable.",
            docsUnavailable: "",
            release: (version) => `Documented release ${version}`,
            results: (count, shown) => shown < count ? `Showing ${shown} of ${count} results` : `${count} result${count === 1 ? "" : "s"}`,
            continueTitle: "Open the API index",
            continueBody: "Browse the complete generated API reference.",
            language: { en: "English", "zh-CN": "中文" },
            filters: {
                language: "Language",
                layer: "Content",
                state: "Stability",
                allLanguages: "All languages",
                allLayers: "All layers",
                allStates: "All states",
                release: "Search release scope"
            },
            layers: { api: "API" },
            states: { stable: "Stable", preview: "Preview", experimental: "Experimental", deprecated: "Deprecated" }
        },
        zh: {
            title: "搜索 Infernux 文档",
            placeholder: "API 符号、模块或方法…",
            close: "关闭文档搜索",
            trigger: "搜索文档",
            triggerText: "搜索文档",
            loading: "正在加载 API 索引…",
            ready: "搜索所选语言，或按稳定性筛选。",
            empty: "没有匹配当前查询与筛选条件的 API。",
            failed: "API 搜索暂时不可用。关闭面板，网络恢复后可重试。",
            apiUnavailable: "API 索引暂不可用。",
            docsUnavailable: "",
            release: (version) => `文档版本 ${version}`,
            results: (count, shown) => shown < count ? `显示 ${shown} / ${count} 条结果` : `${count} 条结果`,
            continueTitle: "打开 API 目录",
            continueBody: "浏览完整的自动生成 API 参考。",
            language: { en: "English", "zh-CN": "中文" },
            filters: {
                language: "语言",
                layer: "内容",
                state: "稳定性",
                allLanguages: "全部语言",
                allLayers: "全部层级",
                allStates: "全部状态",
                release: "检索版本范围"
            },
            layers: { api: "API" },
            states: { stable: "稳定", preview: "预览", experimental: "实验性", deprecated: "已弃用" }
        }
    };

    function copyForLanguage(language) {
        return copyByLanguage[language === "zh" || language === "zh-CN" ? "zh" : "en"];
    }

    const testApi = { buildSearchModel, buildWikiSearchUrl, copyForLanguage, normalized, resultNavigationIndex, score, search };
    if (typeof globalThis !== "undefined" && globalThis.__INFERNUX_DOCS_SEARCH_TEST__) {
        globalThis.__infernuxDocsSearch = testApi;
    }
    if (typeof document === "undefined") return;

    function appendElement(parent, tagName, options = {}) {
        const element = document.createElement(tagName);
        if (options.className) element.className = options.className;
        if (options.id) element.id = options.id;
        if (options.text) element.textContent = options.text;
        for (const [name, value] of Object.entries(options.attributes || {})) element.setAttribute(name, value);
        parent.appendChild(element);
        return element;
    }

    function ensureSearchInterface() {
        let trigger = document.querySelector("[data-docs-search-trigger]");
        let dialog = document.getElementById("docs-search-dialog");
        const navRight = document.querySelector(".nav-right");

        if (!trigger && navRight) {
            trigger = document.createElement("button");
            trigger.type = "button";
            trigger.className = "docs-search-trigger site-docs-search-trigger";
            trigger.dataset.docsSearchTrigger = "";
            trigger.setAttribute("aria-haspopup", "dialog");
            trigger.setAttribute("aria-controls", "docs-search-dialog");
            trigger.setAttribute("aria-expanded", "false");
            const icon = appendElement(trigger, "i", { className: "fas fa-magnifying-glass", attributes: { "aria-hidden": "true" } });
            const label = appendElement(trigger, "span", { attributes: { "data-docs-search-trigger-label": "" } });
            const shortcut = appendElement(trigger, "kbd", { text: "/", attributes: { "aria-hidden": "true" } });
            trigger.append(icon, label, shortcut);
            navRight.prepend(trigger);
        }

        if (!dialog && trigger) {
            dialog = document.createElement("dialog");
            dialog.id = "docs-search-dialog";
            dialog.className = "docs-search-dialog";
            dialog.setAttribute("aria-labelledby", "docs-search-title");

            const backdrop = appendElement(dialog, "button", {
                className: "docs-search-backdrop",
                attributes: { type: "button", "data-docs-search-close": "", tabindex: "-1", "aria-hidden": "true" }
            });
            const panel = appendElement(dialog, "div", { className: "docs-search-panel" });
            const head = appendElement(panel, "div", { className: "docs-search-head" });
            appendElement(head, "h2", { id: "docs-search-title" });
            const close = appendElement(head, "button", {
                className: "docs-search-close",
                attributes: { type: "button", "data-docs-search-close": "" }
            });
            appendElement(close, "i", { className: "fas fa-xmark", attributes: { "aria-hidden": "true" } });
            const field = appendElement(panel, "label", { className: "docs-search-field", attributes: { for: "docs-search-input" } });
            appendElement(field, "i", { className: "fas fa-magnifying-glass", attributes: { "aria-hidden": "true" } });
            appendElement(field, "input", {
                id: "docs-search-input",
                className: "docs-search-input",
                attributes: {
                    type: "search",
                    autocomplete: "off",
                    spellcheck: "false",
                    "aria-controls": "docs-search-results",
                    "aria-describedby": "docs-search-status",
                    "aria-autocomplete": "list"
                }
            });
            appendElement(panel, "div", { id: "docs-search-filters", className: "docs-search-filters", attributes: { "aria-label": "Documentation search filters" } });
            appendElement(panel, "p", { id: "docs-search-status", className: "docs-search-status", attributes: { "aria-live": "polite", "aria-atomic": "true" } });
            appendElement(panel, "div", { id: "docs-search-results", className: "docs-search-results", attributes: { role: "list" } });
            dialog.append(backdrop, panel);
            document.body.appendChild(dialog);
        }

        if (dialog && !dialog.querySelector("#docs-search-wiki-continuation")) {
            const panel = dialog.querySelector(".docs-search-panel");
            if (panel) {
                const continuation = appendElement(panel, "a", {
                    id: "docs-search-wiki-continuation",
                    className: "docs-search-result docs-search-continuation",
                    attributes: { href: "/wiki/site/en/api/index.html", "aria-describedby": "docs-search-status" }
                });
                const top = appendElement(continuation, "span", { className: "docs-search-result-top" });
                appendElement(top, "strong", { attributes: { "data-docs-search-continuation-title": "" } });
                appendElement(top, "i", { className: "fas fa-arrow-right", attributes: { "aria-hidden": "true" } });
                appendElement(continuation, "p", { attributes: { "data-docs-search-continuation-body": "" } });
            }
        }

        return { dialog, trigger };
    }

    function initializeSearchInterface() {
        const { dialog, trigger } = ensureSearchInterface();
        const input = document.getElementById("docs-search-input");
        const filterHost = document.getElementById("docs-search-filters");
        const status = document.getElementById("docs-search-status");
        const results = document.getElementById("docs-search-results");
        if (!trigger || !dialog || !input || !filterHost || !status || !results || trigger.dataset.docsSearchBound === "true") return;
        trigger.dataset.docsSearchBound = "true";

        let language = document.documentElement.lang?.toLowerCase().startsWith("zh") ? "zh" : "en";
        let locale = language === "zh" ? "zh-CN" : "en";
        let copy = copyForLanguage(language);
        let searchModelPromise;
        let activeModel;
        let previousFocus;
        let searchOpen = false;
        const fallbackInertState = new Map();

        function createFilter(id, labelText, options, selectedValue = "all") {
            const label = document.createElement("label");
            label.className = "docs-search-filter";
            label.htmlFor = id;
            const caption = document.createElement("span");
            caption.textContent = labelText;
            const select = document.createElement("select");
            select.id = id;
            for (const [value, text] of options) {
                const option = document.createElement("option");
                option.value = value;
                option.textContent = text;
                option.selected = value === selectedValue;
                select.appendChild(option);
            }
            label.append(caption, select);
            filterHost.appendChild(label);
            return { caption, select };
        }

        const languageControl = createFilter("docs-search-language", copy.filters.language, [
            ["en", "English"], ["zh-CN", "中文"], ["all", copy.filters.allLanguages]
        ], locale);
        const layerControl = createFilter("docs-search-layer", copy.filters.layer, [
            ["all", copy.filters.allLayers], ["api", copy.layers.api]
        ]);
        const statusControl = createFilter("docs-search-state", copy.filters.state, [
            ["all", copy.filters.allStates], ["stable", copy.states.stable], ["preview", copy.states.preview], ["experimental", copy.states.experimental], ["deprecated", copy.states.deprecated]
        ]);
        const languageFilter = languageControl.select;
        const layerFilter = layerControl.select;
        const statusFilter = statusControl.select;
        const releaseScope = document.createElement("p");
        releaseScope.className = "docs-search-release";
        releaseScope.id = "docs-search-release";
        filterHost.appendChild(releaseScope);

        function setOptionText(select, value, text) {
            const option = [...select.options].find((candidate) => candidate.value === value);
            if (option) option.textContent = text;
        }

        function syncLocalizedInterface({ followPageLanguage = false } = {}) {
            language = document.documentElement.lang?.toLowerCase().startsWith("zh") ? "zh" : "en";
            locale = language === "zh" ? "zh-CN" : "en";
            copy = copyForLanguage(language);
            trigger.setAttribute("aria-label", copy.trigger);
            trigger.title = `${copy.trigger} (/)`;
            const triggerLabel = trigger.querySelector("[data-docs-search-trigger-label]") || trigger.querySelector("span");
            if (triggerLabel) triggerLabel.textContent = copy.triggerText;
            dialog.querySelector("h2").textContent = copy.title;
            input.placeholder = copy.placeholder;
            dialog.querySelector(".docs-search-close").setAttribute("aria-label", copy.close);
            filterHost.setAttribute("aria-label", copy.title);
            languageControl.caption.textContent = copy.filters.language;
            layerControl.caption.textContent = copy.filters.layer;
            statusControl.caption.textContent = copy.filters.state;
            setOptionText(languageFilter, "all", copy.filters.allLanguages);
            setOptionText(layerFilter, "all", copy.filters.allLayers);
            for (const [value, text] of Object.entries(copy.layers)) setOptionText(layerFilter, value, text);
            setOptionText(statusFilter, "all", copy.filters.allStates);
            for (const [value, text] of Object.entries(copy.states)) setOptionText(statusFilter, value, text);
            releaseScope.setAttribute("aria-label", copy.filters.release);
            dialog.querySelector("[data-docs-search-continuation-title]").textContent = copy.continueTitle;
            dialog.querySelector("[data-docs-search-continuation-body]").textContent = copy.continueBody;
            if (followPageLanguage) languageFilter.value = locale;
            updateWikiContinuation();
            if (activeModel) render(activeModel);
            else if (searchOpen) status.textContent = copy.loading;
        }

        function loadSearchModel({ retryPartial = false } = {}) {
            if (retryPartial && activeModel && !activeModel.sources.api) {
                searchModelPromise = null;
            }
            if (!searchModelPromise) {
                searchModelPromise = fetch("/api-index.json")
                    .then((response) => {
                        if (!response.ok) throw new Error(`API index HTTP ${response.status}`);
                        return response.json();
                    })
                    .then((apiIndex) => buildSearchModel(apiIndex, null))
                    .then((model) => {
                        activeModel = model;
                        return model;
                    })
                    .catch((error) => {
                        searchModelPromise = null;
                        throw error;
                    });
            }
            return searchModelPromise;
        }

        function resultEyebrow(item) {
            const itemLanguage = copy.language[item.language] || item.language;
            if (item.layer === "api") return `API · ${item.kind} · ${item.module} · ${itemLanguage}`;
            return `${copy.layers[item.layer] || item.layer} · ${itemLanguage}`;
        }

        function statusWithAvailability(message, model) {
            if (!model.sources.api) return `${message} ${copy.apiUnavailable}`;
            if (!model.sources.docs) return `${message} ${copy.docsUnavailable}`;
            return message;
        }

        function updateWikiContinuation() {
            const continuation = dialog.querySelector("#docs-search-wiki-continuation");
            if (!continuation) return;
            continuation.href = buildWikiSearchUrl({
                query: input.value,
                language: languageFilter.value,
                layer: layerFilter.value,
                status: statusFilter.value
            });
        }

        function render(model) {
            results.replaceChildren();
            updateWikiContinuation();
            releaseScope.textContent = copy.release(model.release);
            const outcome = search(model, {
                query: input.value,
                language: languageFilter.value,
                layer: layerFilter.value,
                status: statusFilter.value,
                locale
            });
            const facetActive = layerFilter.value !== "all" || statusFilter.value !== "all";
            if (!input.value.trim() && !facetActive) {
                status.textContent = statusWithAvailability(copy.ready, model);
                return;
            }
            status.textContent = statusWithAvailability(
                outcome.total ? copy.results(outcome.total, outcome.matches.length) : copy.empty,
                model
            );
            for (const [index, { item }] of outcome.matches.entries()) {
                const link = document.createElement("a");
                link.className = "docs-search-result";
                link.href = item.url;
                link.dataset.docSearchResult = "";
                link.setAttribute("role", "listitem");
                link.setAttribute("aria-posinset", String(index + 1));
                link.setAttribute("aria-setsize", String(outcome.matches.length));
                const top = document.createElement("span");
                top.className = "docs-search-result-top";
                const title = document.createElement("strong");
                title.textContent = item.title;
                const badge = document.createElement("span");
                badge.className = "docs-search-result-status";
                badge.textContent = copy.states[item.status] || item.status || "";
                top.append(title, badge);
                const eyebrow = document.createElement("small");
                eyebrow.textContent = resultEyebrow(item);
                const summary = document.createElement("p");
                summary.textContent = item.summary || item.title;
                link.append(top, eyebrow, summary);
                results.appendChild(link);
            }
        }

        function renderFromCurrentState() {
            loadSearchModel().then(render).catch((error) => {
                console.warn(error);
                status.textContent = copy.failed;
            });
        }

        function setFallbackBackgroundInert(inert) {
            if (typeof dialog.showModal === "function") return;
            if (inert) {
                for (const element of document.body.children) {
                    if (element === dialog || !(element instanceof HTMLElement)) continue;
                    fallbackInertState.set(element, element.inert);
                    element.inert = true;
                }
                return;
            }
            for (const [element, previous] of fallbackInertState) element.inert = previous;
            fallbackInertState.clear();
        }

        function finalizeClose() {
            if (!searchOpen) return;
            searchOpen = false;
            dialog.removeAttribute("data-fallback-open");
            document.body.classList.remove("docs-search-open");
            trigger.setAttribute("aria-expanded", "false");
            setFallbackBackgroundInert(false);
            if (previousFocus instanceof HTMLElement) previousFocus.focus();
        }

        function openSearch() {
            if (searchOpen) return;
            previousFocus = document.activeElement;
            searchOpen = true;
            document.dispatchEvent(new CustomEvent("site:docs-search-opened"));
            document.body.classList.add("docs-search-open");
            trigger.setAttribute("aria-expanded", "true");
            status.textContent = copy.loading;
            if (typeof dialog.showModal === "function") dialog.showModal();
            else {
                dialog.setAttribute("data-fallback-open", "true");
                setFallbackBackgroundInert(true);
            }
            input.focus();
            loadSearchModel({ retryPartial: true }).then(render).catch((error) => {
                console.warn(error);
                status.textContent = copy.failed;
            });
        }

        function closeSearch() {
            if (!searchOpen) return;
            if (dialog.open && typeof dialog.close === "function") dialog.close();
            finalizeClose();
        }

        function resultLinks() {
            return [...results.querySelectorAll(".docs-search-result[data-doc-search-result]")];
        }

        function moveResultFocus(event, currentIndex) {
            const links = resultLinks();
            const nextIndex = resultNavigationIndex(currentIndex, event.key, links.length);
            if (nextIndex === currentIndex || nextIndex < 0) return;
            event.preventDefault();
            links[nextIndex].focus();
        }

        syncLocalizedInterface();
        trigger.addEventListener("click", openSearch);
        dialog.querySelectorAll("[data-docs-search-close]").forEach((button) => button.addEventListener("click", closeSearch));
        dialog.addEventListener("cancel", (event) => {
            event.preventDefault();
            closeSearch();
        });
        dialog.addEventListener("close", finalizeClose);
        input.addEventListener("input", renderFromCurrentState);
        input.addEventListener("keydown", (event) => {
            if (event.key === "ArrowDown" || event.key === "ArrowUp") moveResultFocus(event, -1);
        });
        results.addEventListener("keydown", (event) => {
            if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
            const links = resultLinks();
            moveResultFocus(event, links.indexOf(event.target.closest?.(".docs-search-result")));
        });
        for (const filter of [languageFilter, layerFilter, statusFilter]) filter.addEventListener("change", renderFromCurrentState);
        dialog.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                event.preventDefault();
                closeSearch();
            }
            if (event.key === "Tab") {
                const focusable = [...dialog.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), a[href]')].filter((element) => element.offsetParent !== null);
                if (!focusable.length) return;
                const first = focusable[0];
                const last = focusable[focusable.length - 1];
                if (event.shiftKey && document.activeElement === first) {
                    event.preventDefault();
                    last.focus();
                } else if (!event.shiftKey && document.activeElement === last) {
                    event.preventDefault();
                    first.focus();
                }
            }
        });
        document.addEventListener("keydown", (event) => {
            const target = event.target;
            const typing = target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement || target?.isContentEditable;
            const inlineWikiSearch = document.getElementById("wiki-search-input");
            const slashShortcut = event.key === "/" && !typing && !inlineWikiSearch;
            const commandShortcut = (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k";
            if (slashShortcut || commandShortcut) {
                event.preventDefault();
                openSearch();
            }
        });
        document.addEventListener("site:language-changed", () => syncLocalizedInterface({ followPageLanguage: true }));
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initializeSearchInterface);
    else initializeSearchInterface();
})();
