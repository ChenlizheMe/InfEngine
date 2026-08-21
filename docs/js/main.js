/**
 * Infernux Engine - Main JavaScript
 */

// Register the root-scoped offline shell. HTML and machine-readable evidence
// remain network-first inside the worker so a cache cannot silently replace a
// newer authoritative document. An already controlled page never reloads
// without an explicit user action when a replacement worker is ready.
let serviceWorkerUpdateRegistration = null;
let serviceWorkerUpdateWorker = null;
let dismissedServiceWorkerUpdate = null;
let serviceWorkerReloadRequested = false;
let serviceWorkerReloaded = false;
let serviceWorkerControllerBound = false;
let serviceWorkerUpdateState = "ready";
const SERVICE_WORKER_UPDATE_DISMISSED_KEY = "infernux.site-update.dismissed-for-session";

// A reader who chooses Later should not meet the same waiting update again on
// every chapter navigation. The next browser session may announce it again.
function serviceWorkerUpdateDismissedForSession() {
    try {
        return globalThis.sessionStorage?.getItem(SERVICE_WORKER_UPDATE_DISMISSED_KEY) === "true";
    } catch (_) {
        return false;
    }
}

function rememberServiceWorkerUpdateDismissal(dismissed) {
    try {
        if (dismissed) globalThis.sessionStorage?.setItem(SERVICE_WORKER_UPDATE_DISMISSED_KEY, "true");
        else globalThis.sessionStorage?.removeItem(SERVICE_WORKER_UPDATE_DISMISSED_KEY);
    } catch (_) {
        // Storage can be unavailable in privacy-restricted browsing contexts.
    }
}

function serviceWorkerUpdateCopy() {
    const zh = document.documentElement.lang?.toLowerCase().startsWith("zh");
    return zh ? {
        label: "网站更新",
        kicker: "站点同步",
        title: "新版本文档已准备好。",
        body: "刷新后即可使用最新页面与离线文件。",
        apply: "立即更新",
        later: "稍后",
        applying: "正在切换到新版本……"
    } : {
        label: "Website update",
        kicker: "SITE SYNC",
        title: "New documentation is ready.",
        body: "Refresh to use the latest pages and offline files.",
        apply: "Update now",
        later: "Later",
        applying: "Switching to the new version…"
    };
}

function createServiceWorkerUpdateNotice() {
    const existing = document.getElementById("site-update-notice");
    if (existing) return existing;

    const notice = document.createElement("aside");
    notice.id = "site-update-notice";
    notice.className = "site-update-notice";
    notice.hidden = true;

    const copyBlock = document.createElement("div");
    copyBlock.className = "site-update-copy";
    const kicker = document.createElement("span");
    kicker.className = "site-update-kicker";
    kicker.dataset.siteUpdateKicker = "";
    const title = document.createElement("strong");
    title.className = "site-update-title";
    title.dataset.siteUpdateTitle = "";
    const status = document.createElement("p");
    status.id = "site-update-status";
    status.className = "site-update-status";
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    status.setAttribute("aria-atomic", "true");
    copyBlock.append(kicker, title, status);

    const actions = document.createElement("div");
    actions.className = "site-update-actions";
    const applyButton = document.createElement("button");
    applyButton.id = "site-update-apply";
    applyButton.className = "site-update-apply";
    applyButton.type = "button";
    applyButton.addEventListener("click", applyServiceWorkerUpdate);
    const laterButton = document.createElement("button");
    laterButton.id = "site-update-later";
    laterButton.className = "site-update-later";
    laterButton.type = "button";
    laterButton.addEventListener("click", dismissServiceWorkerUpdate);
    actions.append(applyButton, laterButton);

    notice.append(copyBlock, actions);
    document.body.appendChild(notice);
    renderServiceWorkerUpdateNotice();
    return notice;
}

function renderServiceWorkerUpdateNotice() {
    const notice = document.getElementById("site-update-notice");
    if (!notice) return;
    const copy = serviceWorkerUpdateCopy();
    notice.setAttribute("aria-label", copy.label);
    notice.dataset.updateState = serviceWorkerUpdateState;
    notice.querySelector("[data-site-update-kicker]").textContent = copy.kicker;
    notice.querySelector("[data-site-update-title]").textContent = copy.title;
    notice.querySelector("#site-update-status").textContent = serviceWorkerUpdateState === "applying" ? copy.applying : copy.body;
    const applyButton = notice.querySelector("#site-update-apply");
    const laterButton = notice.querySelector("#site-update-later");
    applyButton.textContent = copy.apply;
    laterButton.textContent = copy.later;
    applyButton.disabled = serviceWorkerUpdateState === "applying";
    laterButton.disabled = serviceWorkerUpdateState === "applying";
}

function showServiceWorkerUpdate(registration, worker = registration?.waiting) {
    if (!worker || worker === dismissedServiceWorkerUpdate || serviceWorkerUpdateDismissedForSession()) return;
    serviceWorkerUpdateRegistration = registration;
    serviceWorkerUpdateWorker = worker;
    serviceWorkerUpdateState = "ready";
    const notice = createServiceWorkerUpdateNotice();
    renderServiceWorkerUpdateNotice();
    notice.hidden = false;
}

function dismissServiceWorkerUpdate() {
    dismissedServiceWorkerUpdate = serviceWorkerUpdateWorker;
    rememberServiceWorkerUpdateDismissal(true);
    const notice = document.getElementById("site-update-notice");
    if (notice) notice.hidden = true;
}

function applyServiceWorkerUpdate() {
    const worker = serviceWorkerUpdateRegistration?.waiting || serviceWorkerUpdateWorker;
    if (!worker || serviceWorkerUpdateState === "applying") return;
    serviceWorkerReloadRequested = true;
    rememberServiceWorkerUpdateDismissal(false);
    serviceWorkerUpdateState = "applying";
    renderServiceWorkerUpdateNotice();
    worker.postMessage("SKIP_WAITING");
}

function handleServiceWorkerControllerChange() {
    if (!serviceWorkerReloadRequested || serviceWorkerReloaded) return;
    serviceWorkerReloaded = true;
    window.location.reload();
}

function monitorServiceWorkerUpdates(registration) {
    serviceWorkerUpdateRegistration = registration;
    if (!serviceWorkerControllerBound) {
        navigator.serviceWorker.addEventListener("controllerchange", handleServiceWorkerControllerChange);
        serviceWorkerControllerBound = true;
    }
    if (registration.waiting && navigator.serviceWorker.controller) {
        showServiceWorkerUpdate(registration, registration.waiting);
    }
    registration.addEventListener("updatefound", () => {
        const installing = registration.installing;
        if (!installing) return;
        installing.addEventListener("statechange", () => {
            if (installing.state === "installed" && navigator.serviceWorker.controller) {
                showServiceWorkerUpdate(registration, registration.waiting || installing);
            }
        });
    });
}

async function registerOfflineShell() {
    try {
        const registration = await navigator.serviceWorker.register("/sw.js", {
            scope: "/",
            updateViaCache: "none"
        });
        monitorServiceWorkerUpdates(registration);
        registration.update().catch(() => {});
    } catch (error) {
        console.warn("Infernux offline shell could not be registered.", error);
    }
}

const serviceWorkerHostname = window.location?.hostname || "";
const canRegisterOfflineShell = "serviceWorker" in navigator
    && (window.isSecureContext || serviceWorkerHostname === "localhost" || serviceWorkerHostname === "127.0.0.1");
if (canRegisterOfflineShell && !globalThis.__INFERNUX_SW_UPDATE_TEST__) {
    window.addEventListener("load", registerOfflineShell);
}

// ── Theme toggle ─────────────────────────────
const SITE_THEME_COLORS = Object.freeze({ dark: "#0a0c11", light: "#f4f1e8" });

function updateThemeColor(theme) {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", SITE_THEME_COLORS[theme] || SITE_THEME_COLORS.dark);
}

function toggleTheme() {
    const html = document.documentElement;
    const current = html.getAttribute('data-theme');
    const next = current === 'light' ? 'dark' : 'light';
    html.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
    updateThemeColor(next);
    updateThemeIcon(next);
    applyNavbarBackground();
    document.dispatchEvent(new CustomEvent('site:theme-changed', { detail: { theme: next } }));
}

function updateThemeIcon(theme) {
    const icon = document.getElementById('theme-icon');
    const button = document.querySelector('.theme-toggle');
    if (icon) {
        icon.className = theme === 'light' ? 'fas fa-sun' : 'fas fa-moon';
    }
    if (button) {
        const isLight = theme === 'light';
        const zh = document.documentElement.lang?.toLowerCase().startsWith('zh');
        button.setAttribute('aria-pressed', String(isLight));
        const label = isLight
            ? (zh ? '切换到深色主题' : 'Switch to dark theme')
            : (zh ? '切换到浅色主题' : 'Switch to light theme');
        button.setAttribute('aria-label', label);
        button.title = label;
    }
}

// Apply saved theme on load
(function() {
    const saved = localStorage.getItem('theme') || 'dark';
    if (saved === 'light') {
        document.documentElement.setAttribute('data-theme', 'light');
    }
    updateThemeColor(saved);
    document.addEventListener('DOMContentLoaded', function() {
        updateThemeIcon(saved);
    });
})();

// Mobile menu toggle
function toggleMobileMenu() {
    const navLinks = document.querySelector('.nav-links');
    if (!navLinks) return;
    const open = !navLinks.classList.contains('mobile-open');
    setMobileMenuState(open, { moveFocus: open });
}

function bindSiteActions() {
    const handlers = {
        theme: toggleTheme,
        language: () => {
            if (typeof toggleLanguage === 'function') toggleLanguage();
        },
        menu: toggleMobileMenu
    };
    document.querySelectorAll('[data-site-action]').forEach(control => {
        const action = control.dataset.siteAction;
        if (!handlers[action] || control.dataset.siteActionBound === 'true') return;
        control.dataset.siteActionBound = 'true';
        control.addEventListener('click', handlers[action]);
    });
}

function mobileMenuElements() {
    const navLinks = document.querySelector('.nav-links');
    const button = document.querySelector('.mobile-menu-btn');
    return { navLinks, button };
}

function mobileMenuFocusables() {
    const { navLinks, button } = mobileMenuElements();
    if (!navLinks || !button) return [];
    return [button, ...navLinks.querySelectorAll('a[href]')];
}

function setMobileMenuState(open, { moveFocus = false, returnFocus = false } = {}) {
    const { navLinks, button } = mobileMenuElements();
    if (!navLinks || !button) return;
    const nextOpen = Boolean(open && window.innerWidth <= 1180);
    navLinks.classList.toggle('mobile-open', nextOpen);
    document.body?.classList.toggle('mobile-menu-open', nextOpen);
    button.setAttribute('aria-expanded', String(nextOpen));
    const zh = document.documentElement.lang?.toLowerCase().startsWith('zh');
    button.setAttribute('aria-label', nextOpen
        ? (zh ? '关闭导航菜单' : 'Close navigation menu')
        : (zh ? '打开导航菜单' : 'Open navigation menu'));
    const icon = button.querySelector('i');
    if (icon) icon.className = nextOpen ? 'fas fa-xmark' : 'fas fa-bars';
    if (nextOpen && moveFocus) {
        window.requestAnimationFrame(() => navLinks.querySelector('a[href]')?.focus({ preventScroll: true }));
    } else if (!nextOpen && returnFocus) {
        button.focus({ preventScroll: true });
    }
}

function handleMobileMenuKeydown(event) {
    const { navLinks } = mobileMenuElements();
    if (!navLinks?.classList.contains('mobile-open')) return;
    if (event.key === 'Escape') {
        event.preventDefault();
        setMobileMenuState(false, { returnFocus: true });
        return;
    }
    if (event.key !== 'Tab') return;
    const focusables = mobileMenuFocusables();
    if (focusables.length < 2) return;
    const first = focusables[0];
    const last = focusables.at(-1);
    if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus({ preventScroll: true });
    } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus({ preventScroll: true });
    }
}

function handleMobileMenuPointerDown(event) {
    const { navLinks } = mobileMenuElements();
    if (!navLinks?.classList.contains('mobile-open')) return;
    if (!event.target?.closest?.('.navbar')) setMobileMenuState(false);
}

// Smooth scroll for anchor links
document.addEventListener('DOMContentLoaded', function() {
    bindSiteActions();
    applyNavbarBackground();
    document.querySelectorAll('.nav-links a').forEach(link => {
        link.addEventListener('click', () => setMobileMenuState(false));
    });
    const links = document.querySelectorAll('a[href^="#"]');
    
    links.forEach(link => {
        link.addEventListener('click', function(e) {
            const href = this.getAttribute('href');
            if (href === '#') return;
            
            const target = document.querySelector(href);
            if (target) {
                e.preventDefault();
                const navHeight = document.querySelector('.navbar').offsetHeight;
                const targetPosition = target.getBoundingClientRect().top + window.pageYOffset - navHeight - 20;
                
                window.scrollTo({
                    top: targetPosition,
                    behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'
                });
            }
        });
    });
});

// Navbar state remains class-driven so theme, forced-colors and future design
// tokens can resolve the final surface in CSS.
function applyNavbarBackground() {
    const navbar = document.querySelector('.navbar');
    if (!navbar) return;
    navbar.classList.toggle('is-scrolled', window.scrollY > 50);
}

window.addEventListener('scroll', applyNavbarBackground);
window.addEventListener('resize', () => {
    if (window.innerWidth > 1180) setMobileMenuState(false);
});
document.addEventListener('keydown', handleMobileMenuKeydown);
document.addEventListener('pointerdown', handleMobileMenuPointerDown);
document.addEventListener('site:language-changed', () => {
    const theme = document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
    updateThemeIcon(theme);
    const zh = document.documentElement.lang?.toLowerCase().startsWith('zh');
    const languageButton = document.querySelector('.lang-toggle');
    if (languageButton) languageButton.setAttribute('aria-label', zh ? '切换到英文' : 'Switch to Chinese');
    const skipLink = document.querySelector('.skip-link');
    if (skipLink) skipLink.textContent = zh ? '跳到正文' : 'Skip to content';
    renderServiceWorkerUpdateNotice();
    setMobileMenuState(false);
});
document.addEventListener('site:docs-search-opened', () => setMobileMenuState(false));

// Add animation classes when elements come into view
const observerOptions = {
    root: null,
    rootMargin: '0px',
    threshold: 0.1
};

const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const observer = !reduceMotion && 'IntersectionObserver' in window
    ? new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                entry.target.classList.remove('reveal-pending');
                entry.target.classList.add('animate-in');
                observer.unobserve(entry.target);
            }
        });
    }, observerOptions)
    : null;

document.addEventListener('DOMContentLoaded', function() {
    const animatedElements = document.querySelectorAll('[data-reveal], .hero-slab, .subpage-hero, .hub-hero, .cta-panel');
    animatedElements.forEach(el => {
        if (!observer) {
            el.classList.add('animate-in');
            return;
        }
        el.classList.add('reveal-pending');
        observer.observe(el);
    });
});

if (globalThis.__INFERNUX_NAV_TEST__) {
    globalThis.__infernuxNavigation = {
        handleMobileMenuKeydown,
        handleMobileMenuPointerDown,
        bindSiteActions,
        mobileMenuFocusables,
        setMobileMenuState,
        toggleMobileMenu
    };
}

if (globalThis.__INFERNUX_SW_UPDATE_TEST__) {
    globalThis.__infernuxServiceWorkerUpdate = {
        applyServiceWorkerUpdate,
        dismissServiceWorkerUpdate,
        getState: () => ({
            dismissedWorker: dismissedServiceWorkerUpdate,
            reloadRequested: serviceWorkerReloadRequested,
            reloaded: serviceWorkerReloaded,
            updateState: serviceWorkerUpdateState,
            worker: serviceWorkerUpdateWorker
        }),
        handleServiceWorkerControllerChange,
        monitorServiceWorkerUpdates,
        registerOfflineShell,
        renderServiceWorkerUpdateNotice,
        showServiceWorkerUpdate
    };
}
