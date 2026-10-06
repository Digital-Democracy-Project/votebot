// Run with: npm test
//
// widget.js is an IIFE with nothing exported, so the test loads its source in a vm with a
// stubbed window/document (readyState "loading", so initWidget never runs) and rewrites the
// closing line to expose the two pure functions under test. Every test runs against both
// src/widget.js and the committed dist/ddp-chat.min.js (what actually ships), so a stale
// rebuild fails here.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const TARGETS = { src: ['src', 'widget.js'], dist: ['dist', 'ddp-chat.min.js'] };

function loadWidget(target, pageContext) {
    const source = fs
        .readFileSync(path.join(__dirname, '..', ...TARGETS[target]), 'utf8')
        .replace(/\}\)\(\);\s*$/, 'window.__t = { resolvePageContext, contextChanged };\n})();');
    const window = {
        DDPChatConfig: { wsUrl: 'wss://example.test/ws/chat', pageContext },
        location: { search: '', href: 'https://example.test/', pathname: '/', hostname: 'example.test' },
    };
    window.window = window;
    const context = vm.createContext({
        window,
        document: { readyState: 'loading', addEventListener() {}, createElement: () => ({ style: {} }), head: { appendChild() {} } },
        console: { log() {}, warn() {}, error() {} },
        URLSearchParams,
    });
    vm.runInContext(source, context);
    return window.__t;
}

const BILL = 'a3f7c0d1-1111-4222-8333-444455556666';

for (const target of Object.keys(TARGETS)) test(`explicit pageContext keeps ocd_bill_id, url and session-code [${target}]`, () => {
    const { resolvePageContext } = loadWidget(target, {
        type: 'bill',
        id: 'HB 219',
        jurisdiction: 'FL',
        'session-code': '2026',
        ocd_bill_id: BILL,
        url: 'https://example.test/explore/FL/2026/HB%20219',
    });
    const ctx = resolvePageContext();
    assert.strictEqual(ctx.ocd_bill_id, BILL);
    assert.strictEqual(ctx.session, '2026');
    assert.strictEqual(ctx.url, 'https://example.test/explore/FL/2026/HB%20219');
    assert.strictEqual(ctx.id, 'HB 219');
});

for (const target of Object.keys(TARGETS)) test(`Webflow-era pageContext is normalized exactly as before [${target}]`, () => {
    const { resolvePageContext } = loadWidget(target, {
        type: 'bill',
        billId: 'HR 1',
        jurisdictionIso2: 'US',
        sessionCode: '119',
        slug: 'a-bill',
    });
    assert.deepStrictEqual(JSON.parse(JSON.stringify(resolvePageContext())), {
        type: 'bill',
        title: null,
        slug: 'a-bill',
        id: 'HR 1',
        jurisdiction: 'US',
        session: '119',
    });
});

for (const target of Object.keys(TARGETS)) test(`navigating between two bills with equal id and no slug is a change [${target}]`, () => {
    const { contextChanged } = loadWidget(target, null);
    const a = { type: 'bill', id: 'HB 1', ocd_bill_id: BILL };
    const b = { type: 'bill', id: 'HB 1', ocd_bill_id: 'b4a8d1e2-2222-4333-8444-555566667777' };
    assert.strictEqual(contextChanged(a, b), true);
    assert.strictEqual(contextChanged(a, { ...a }), false);
});

for (const target of Object.keys(TARGETS)) test(`Webflow-era comparisons are unchanged [${target}]`, () => {
    const { contextChanged } = loadWidget(target, null);
    assert.strictEqual(contextChanged({ type: 'bill', slug: 'a' }, { type: 'bill', slug: 'b' }), true);
    assert.strictEqual(contextChanged({ type: 'bill', slug: 'a' }, { type: 'bill', slug: 'a' }), false);
    assert.strictEqual(contextChanged({ type: 'bill', id: 'HB 1' }, { type: 'legislator', id: 'HB 1' }), true);
});

for (const target of Object.keys(TARGETS)) test(`gaining or losing an ocd_bill_id is a change [${target}]`, () => {
    const { contextChanged } = loadWidget(target, null);
    const withId = { type: 'bill', id: 'HB 1', ocd_bill_id: BILL };
    const withoutId = { type: 'bill', id: 'HB 1' };
    assert.strictEqual(contextChanged(withoutId, withId), true);
    assert.strictEqual(contextChanged(withId, withoutId), true);
});

for (const target of Object.keys(TARGETS)) test(`the same ocd_bill_id is the same bill whatever else differs [${target}]`, () => {
    const { contextChanged } = loadWidget(target, null);
    const a = { type: 'bill', id: 'HB 1', slug: 'a', ocd_bill_id: BILL };
    assert.strictEqual(contextChanged(a, { ...a, slug: 'b', title: 'Renamed' }), false);
    assert.strictEqual(contextChanged(a, { ...a, type: 'organization' }), true);
});
