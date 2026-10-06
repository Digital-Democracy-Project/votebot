// Run with: node --test chat-widget/test
//
// widget.js is an IIFE with nothing exported, so the test loads its source in a vm with a
// stubbed window/document (readyState "loading", so initWidget never runs) and appends a line
// that exposes the two pure functions under test.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function loadWidget(pageContext) {
    const source = fs
        .readFileSync(path.join(__dirname, '..', 'src', 'widget.js'), 'utf8')
        .replace(/\}\)\(\);\s*$/, 'window.__t = { resolvePageContext, contextChanged };\n})();');
    const window = {
        DDPChatConfig: { wsUrl: 'wss://example.test/ws/chat', pageContext },
        location: { search: '', href: 'https://example.test/', pathname: '/', hostname: 'example.test' },
    };
    window.window = window;
    const context = vm.createContext({
        window,
        document: { readyState: 'loading', addEventListener() {} },
        console: { log() {}, warn() {}, error() {} },
        URLSearchParams,
    });
    vm.runInContext(source, context);
    return window.__t;
}

const BILL = 'a3f7c0d1-1111-4222-8333-444455556666';

test('explicit pageContext keeps ocd_bill_id, url and session-code', () => {
    const { resolvePageContext } = loadWidget({
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

test('Webflow-era pageContext is normalized exactly as before', () => {
    const { resolvePageContext } = loadWidget({
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

test('navigating between two bills with equal id and no slug is a change', () => {
    const { contextChanged } = loadWidget(null);
    const a = { type: 'bill', id: 'HB 1', ocd_bill_id: BILL };
    const b = { type: 'bill', id: 'HB 1', ocd_bill_id: 'b4a8d1e2-2222-4333-8444-555566667777' };
    assert.strictEqual(contextChanged(a, b), true);
    assert.strictEqual(contextChanged(a, { ...a }), false);
});

test('Webflow-era comparisons are unchanged', () => {
    const { contextChanged } = loadWidget(null);
    assert.strictEqual(contextChanged({ type: 'bill', slug: 'a' }, { type: 'bill', slug: 'b' }), true);
    assert.strictEqual(contextChanged({ type: 'bill', slug: 'a' }, { type: 'bill', slug: 'a' }), false);
    assert.strictEqual(contextChanged({ type: 'bill', id: 'HB 1' }, { type: 'legislator', id: 'HB 1' }), true);
});
