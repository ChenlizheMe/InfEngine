import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const source = await readFile(path.join(scriptDir, "..", "js", "main.js"), "utf8");

function classList() {
    const values = new Set();
    return {
        add(...names) { names.forEach((name) => values.add(name)); },
        contains(name) { return values.has(name); },
        remove(...names) { names.forEach((name) => values.delete(name)); },
        toggle(name, force) {
            const enabled = force === undefined ? !values.has(name) : Boolean(force);
            if (enabled) values.add(name);
            else values.delete(name);
            return enabled;
        }
    };
}

function createEventTarget(initial = {}) {
    const listeners = new Map();
    return Object.assign(initial, {
        addEventListener(type, handler) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(handler);
        },
        dispatch(type, event = {}) {
            for (const handler of listeners.get(type) || []) handler(event);
        },
        listenerCount(type) { return (listeners.get(type) || []).length; }
    });
}

function createElement(tagName, registry) {
    const attributes = new Map();
    const element = createEventTarget({
        attributes,
        children: [],
        classList: classList(),
        className: "",
        dataset: {},
        disabled: false,
        hidden: false,
        id: "",
        tagName: tagName.toUpperCase(),
        textContent: "",
        append(...children) {
            children.forEach((child) => this.appendChild(child));
        },
        appendChild(child) {
            this.children.push(child);
            if (child.id) registry.set(child.id, child);
            return child;
        },
        getAttribute(name) { return attributes.get(name) ?? null; },
        querySelector(selector) {
            const matches = (candidate) => {
                if (selector.startsWith("#")) return candidate.id === selector.slice(1);
                const dataMatch = selector.match(/^\[data-([a-z-]+)\]$/);
                if (!dataMatch) return false;
                const key = dataMatch[1].replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
                return Object.hasOwn(candidate.dataset, key);
            };
            const queue = [...this.children];
            while (queue.length) {
                const candidate = queue.shift();
                if (matches(candidate)) return candidate;
                queue.push(...candidate.children);
            }
            return null;
        },
        setAttribute(name, value) {
            attributes.set(name, String(value));
            if (name === "id") {
                this.id = String(value);
                registry.set(this.id, this);
            }
        }
    });
    return element;
}

function createWorker() {
    return createEventTarget({
        messages: [],
        state: "installing",
        postMessage(message) { this.messages.push(message); }
    });
}

function runScenario({ controlled = true, sessionEntries = [] } = {}) {
    const registry = new Map();
    const documentListeners = new Map();
    const body = createElement("body", registry);
    body.classList = classList();
    const documentElement = {
        lang: "en",
        attributes: new Map(),
        getAttribute(name) { return this.attributes.get(name) ?? null; },
        setAttribute(name, value) { this.attributes.set(name, value); }
    };
    const document = {
        activeElement: null,
        body,
        documentElement,
        addEventListener(type, handler) {
            if (!documentListeners.has(type)) documentListeners.set(type, []);
            documentListeners.get(type).push(handler);
        },
        createElement(tagName) { return createElement(tagName, registry); },
        dispatchEvent() {},
        getElementById(id) { return registry.get(id) || null; },
        querySelector() { return null; },
        querySelectorAll() { return []; }
    };
    const serviceWorker = createEventTarget({
        controller: controlled ? {} : null,
        registrations: [],
        async register(url, options) {
            this.registrations.push({ options, url });
            return this.nextRegistration;
        }
    });
    let reloads = 0;
    const window = createEventTarget({
        innerWidth: 1440,
        isSecureContext: true,
        location: {
            hostname: "infernux-engine.com",
            reload() { reloads += 1; }
        },
        matchMedia() { return { matches: true }; },
        pageYOffset: 0,
        requestAnimationFrame(callback) { callback(); },
        scrollY: 0
    });
    const sessionValues = new Map(sessionEntries);
    const sandbox = {
        __INFERNUX_SW_UPDATE_TEST__: true,
        CustomEvent: class {},
        console,
        document,
        globalThis: null,
        localStorage: { getItem() { return null; }, setItem() {} },
        sessionStorage: {
            getItem(key) { return sessionValues.get(key) ?? null; },
            removeItem(key) { sessionValues.delete(key); },
            setItem(key, value) { sessionValues.set(key, String(value)); }
        },
        navigator: { serviceWorker },
        setTimeout() {},
        window
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    new vm.Script(source, { filename: "main.js" }).runInContext(sandbox);
    return {
        api: sandbox.__infernuxServiceWorkerUpdate,
        document,
        documentListeners,
        getReloads: () => reloads,
        sessionValues,
        serviceWorker
    };
}

function createRegistration({ installing = null, waiting = null } = {}) {
    return createEventTarget({
        installing,
        updateCalls: 0,
        waiting,
        async update() { this.updateCalls += 1; }
    });
}

const waitingScenario = runScenario();
const firstWorker = createWorker();
const registration = createRegistration({ waiting: firstWorker });
waitingScenario.api.monitorServiceWorkerUpdates(registration);

const notice = waitingScenario.document.getElementById("site-update-notice");
assert.ok(notice, "a waiting replacement should create one shared update notice");
assert.equal(notice.hidden, false);
assert.equal(notice.querySelector("[data-site-update-title]").textContent, "New documentation is ready.");
assert.equal(notice.querySelector("#site-update-apply").disabled, false);
assert.equal(waitingScenario.serviceWorker.listenerCount("controllerchange"), 1);

waitingScenario.api.dismissServiceWorkerUpdate();
assert.equal(notice.hidden, true);
waitingScenario.api.showServiceWorkerUpdate(registration, firstWorker);
assert.equal(notice.hidden, true, "dismissal should suppress the notice for the rest of this browsing session");

const secondWorker = createWorker();
registration.waiting = secondWorker;
waitingScenario.api.showServiceWorkerUpdate(registration, secondWorker);
assert.equal(notice.hidden, true, "page navigation or a later worker must not repeat a dismissed notice in the same session");
waitingScenario.sessionValues.clear();
waitingScenario.api.showServiceWorkerUpdate(registration, secondWorker);
assert.equal(notice.hidden, false, "a new browsing session may announce the waiting update");
waitingScenario.document.documentElement.lang = "zh-CN";
waitingScenario.api.renderServiceWorkerUpdateNotice();
assert.equal(notice.getAttribute("aria-label"), "网站更新");
assert.equal(notice.querySelector("[data-site-update-title]").textContent, "新版本文档已准备好。");

waitingScenario.api.applyServiceWorkerUpdate();
waitingScenario.api.applyServiceWorkerUpdate();
assert.deepEqual(secondWorker.messages, ["SKIP_WAITING"], "activation must require one explicit user action");
assert.equal(waitingScenario.api.getState().updateState, "applying");
assert.equal(notice.querySelector("#site-update-apply").disabled, true);
assert.equal(notice.querySelector("#site-update-status").textContent, "正在切换到新版本……");

waitingScenario.serviceWorker.dispatch("controllerchange");
waitingScenario.serviceWorker.dispatch("controllerchange");
assert.equal(waitingScenario.getReloads(), 1, "controller replacement should reload exactly once after consent");

const freshInstallScenario = runScenario({ controlled: false });
const freshWorker = createWorker();
freshInstallScenario.api.monitorServiceWorkerUpdates(createRegistration({ waiting: freshWorker }));
assert.equal(freshInstallScenario.document.getElementById("site-update-notice"), null, "a first install must not ask the user to refresh");
freshInstallScenario.serviceWorker.dispatch("controllerchange");
assert.equal(freshInstallScenario.getReloads(), 0, "controller changes without user consent must not reload");

const lifecycleScenario = runScenario();
const installingWorker = createWorker();
const lifecycleRegistration = createRegistration({ installing: installingWorker });
lifecycleScenario.api.monitorServiceWorkerUpdates(lifecycleRegistration);
lifecycleRegistration.dispatch("updatefound");
assert.equal(lifecycleScenario.document.getElementById("site-update-notice"), null);
installingWorker.state = "installed";
installingWorker.dispatch("statechange");
assert.equal(lifecycleScenario.document.getElementById("site-update-notice").hidden, false, "an installed replacement should be announced");

const registrationScenario = runScenario();
const registeredWorker = createWorker();
const registered = createRegistration({ waiting: registeredWorker });
registrationScenario.serviceWorker.nextRegistration = registered;
await registrationScenario.api.registerOfflineShell();
assert.equal(registrationScenario.serviceWorker.registrations.length, 1);
assert.equal(registrationScenario.serviceWorker.registrations[0].url, "/sw.js");
assert.equal(registrationScenario.serviceWorker.registrations[0].options.scope, "/");
assert.equal(registrationScenario.serviceWorker.registrations[0].options.updateViaCache, "none");
await Promise.resolve();
assert.equal(registered.updateCalls, 1);

assert.doesNotMatch(source, /\.style(?:\.|\[|\s*=)|\.innerHTML\s*=/);
console.log("Service Worker update UX test passed: waiting/updatefound detection, bilingual notice, dismissal, explicit activation, and consent-gated single reload.");
