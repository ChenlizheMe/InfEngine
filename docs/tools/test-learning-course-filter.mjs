import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

const source = await readFile("docs/js/learn.js", "utf8");

function eventTarget(initial = {}) {
    const listeners = new Map();
    return Object.assign(initial, {
        addEventListener(type, handler) {
            if (!listeners.has(type)) listeners.set(type, []);
            listeners.get(type).push(handler);
        },
        dispatch(type) {
            for (const handler of listeners.get(type) || []) handler({ target: this });
        }
    });
}

function entry(searchZh, searchEn, tags) {
    return { dataset: { searchZh, searchEn, tags }, hidden: false };
}

function button(tag) {
    const classes = new Set(tag === "all" ? ["is-active"] : []);
    return eventTarget({
        dataset: { learnTag: tag },
        classList: { toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); } },
        setAttribute() {}
    });
}

function panel(items) {
    const input = eventTarget({ value: "" });
    const status = { textContent: "" };
    return {
        hidden: false,
        input,
        items,
        status,
        querySelector(selector) {
            if (selector === "[data-learn-search]") return input;
            if (selector === "[data-learn-status]") return status;
            return null;
        },
        querySelectorAll(selector) { return selector === "[data-learn-entry]" ? items : []; }
    };
}

const zhItems = [entry("物理与碰撞", "Physics and collision", "physics"), entry("协程", "Coroutines", "coroutine")];
const enItems = [entry("物理与碰撞", "Physics and collision", "physics"), entry("协程", "Coroutines", "coroutine")];
const zhPanel = panel(zhItems);
const enPanel = panel(enItems);
enPanel.hidden = true;
const buttons = [button("all"), button("physics")];
const documentTarget = eventTarget({
    documentElement: { lang: "zh-CN" },
    querySelector(selector) {
        if (selector === "[data-page-language]:not([hidden])") return zhPanel.hidden ? enPanel : zhPanel;
        return null;
    },
    querySelectorAll(selector) {
        if (selector === "[data-learn-search]") return [enPanel.input, zhPanel.input];
        if (selector === "[data-learn-tag]") return buttons;
        return [];
    }
});

const sandbox = { document: documentTarget };
vm.createContext(sandbox);
new vm.Script(source, { filename: "learn.js" }).runInContext(sandbox);
documentTarget.dispatch("DOMContentLoaded");

zhPanel.input.value = "物理";
zhPanel.input.dispatch("input");
assert.equal(zhItems.filter((item) => !item.hidden).length, 1);
assert.equal(zhPanel.status.textContent, "找到 1 个章节");
assert.equal(enPanel.input.value, "物理", "the query should follow the reader across language panels");

zhPanel.input.value = "";
zhPanel.input.dispatch("search");
assert.equal(zhItems.filter((item) => !item.hidden).length, 2, "the native search clear action must restore every chapter");
assert.equal(zhPanel.status.textContent, "找到 2 个章节");

buttons[1].dispatch("click");
assert.equal(zhItems.filter((item) => !item.hidden).length, 1);
buttons[0].dispatch("click");
assert.equal(zhItems.filter((item) => !item.hidden).length, 2, "All must clear the active tag without a page reload");

zhPanel.hidden = true;
enPanel.hidden = false;
documentTarget.documentElement.lang = "en";
documentTarget.dispatch("site:language-changed");
assert.equal(enPanel.status.textContent, "2 chapters");

console.log("Learning course filter test passed: native clear, tag reset, synchronized query, and language switch.");
