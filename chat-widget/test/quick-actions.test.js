// Run with: npm test
//
// VOTEBOT-58: the Summary / Pros & cons / Status & votes buttons must work however the bill context
// arrives. A host that learns the bill late (ddp-next, a single-page app) starts the widget on a
// general context and calls DDPChatWidget.setPageContext(bill) afterwards; the buttons then showed
// but did nothing, because the click handlers were wired only when the widget STARTED on a bill.
//
// Like page-context.test.js this loads src/widget.js and the committed dist/ddp-chat.min.js in a vm
// with readyState "loading" (initWidget never runs). The three modules the widget drives (DDPUI,
// DDPChat, DDPWebSocket) are stand-ins that record calls: in src they are globals the test
// provides, in dist they are the bundle's own objects with the methods under test replaced.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const TARGETS = { src: ['src', 'widget.js'], dist: ['dist', 'ddp-chat.min.js'] };
const BILL = { type: 'bill', id: 'HB 1', title: 'A Bill', ocd_bill_id: 'a3f7c0d1-1111-4222-8333-444455556666' };
const GENERAL = { type: 'general' };
const tick = () => new Promise((resolve) => setImmediate(resolve));

function loadWidget(target, { features = { quick_action_buttons_enabled: true }, fetchFails = false } = {}) {
    const rec = { fetches: 0, sent: [], handler: null, wired: 0, shown: 0, hidden: 0, enabled: 0, context: GENERAL, release: null };
    const stubs = {
        DDPUI: {
            showQuickActions() { rec.shown++; },
            hideQuickActions() { rec.hidden++; },
            enableQuickActions() { rec.enabled++; },
            setupQuickActionHandlers(fn) { rec.wired++; rec.handler = fn; },
            addSystemMessage() {},
        },
        DDPChat: {
            getPageContext() { return rec.context; },
            setPageContext(ctx) { rec.context = ctx; },
            sendMessage(message, button) { rec.sent.push([message, button]); },
        },
        DDPWebSocket: { storageSet() {} },
    };
    // the features request answers only when the test says so, so "the bill arrives while it is in flight" can be tested
    const fetch = () => {
        rec.fetches++;
        return new Promise((resolve, reject) => {
            rec.release = () => (fetchFails ? reject(new Error('offline')) : resolve({ ok: true, json: async () => features }));
        });
    };
    const source = fs
        .readFileSync(path.join(__dirname, '..', ...TARGETS[target]), 'utf8')
        .replace(/\}\)\(\);\s*$/, 'window.__t = { fetchFeatureFlagsAndMaybeShowButtons };\n})();');
    const window = {
        DDPChatConfig: { wsUrl: 'wss://example.test/ws/chat', pageContext: GENERAL },
        location: { search: '', href: 'https://example.test/', pathname: '/', hostname: 'example.test' },
    };
    window.window = window;
    const sandbox = {
        window,
        document: { readyState: 'loading', addEventListener() {}, createElement: () => ({ style: {} }), head: { appendChild() {} } },
        console: { log() {}, warn() {}, error() {} },
        URLSearchParams,
        fetch,
    };
    if (target === 'src') Object.assign(sandbox, stubs);
    const context = vm.createContext(sandbox);
    vm.runInContext(source, context);
    if (target === 'dist') {
        // the bundle's own modules are lexical globals of the script: keep them, swap in the recorders
        for (const name of Object.keys(stubs)) Object.assign(vm.runInContext(name, context), stubs[name]);
    }
    return { widget: window.DDPChatWidget, startFetch: window.__t.fetchFeatureFlagsAndMaybeShowButtons, rec };
}

async function featuresArrive(rec) {
    rec.release();
    await tick();
}

function click(rec, button) {
    assert.ok(rec.handler, 'the buttons have a click handler');
    rec.handler(button);
}

for (const target of Object.keys(TARGETS)) {
    test(`a bill context that arrives after the widget started general gets working buttons [${target}]`, async () => {
        const { widget, startFetch, rec } = loadWidget(target);
        startFetch(); // init on a general page: the features arrive, no bill yet
        await featuresArrive(rec);
        assert.strictEqual(rec.wired, 0);

        widget.setPageContext(BILL, true);

        assert.strictEqual(rec.wired, 1);
        assert.ok(rec.shown >= 1);
        click(rec, 'summary');
        click(rec, 'pros_cons');
        click(rec, 'status_votes');
        assert.deepStrictEqual(rec.sent, [
            ['Summarize this bill', 'summary'],
            ['What are the pros and cons of this bill?', 'pros_cons'],
            ['What is the latest status and vote history for A Bill?', 'status_votes'],
        ]);
    });

    test(`moving bill, then general, then a bill again wires the handlers once, so a click sends once [${target}]`, async () => {
        const { widget, startFetch, rec } = loadWidget(target);
        startFetch();
        await featuresArrive(rec);

        widget.setPageContext(BILL, true);
        widget.setPageContext(GENERAL, true);
        const hiddenOnGeneral = rec.hidden;
        widget.setPageContext({ ...BILL, id: 'HB 2', ocd_bill_id: 'b4a8d1e2-2222-4333-8444-555566667777' }, true);

        assert.ok(hiddenOnGeneral >= 1, 'the bar is hidden on a general page');
        assert.strictEqual(rec.wired, 1);
        click(rec, 'summary');
        assert.strictEqual(rec.sent.length, 1);
    });

    test(`a bill that arrives while the features request is in flight does not start a second request [${target}]`, async () => {
        const { widget, startFetch, rec } = loadWidget(target);
        startFetch();
        widget.setPageContext(BILL, true);
        widget.setPageContext(BILL, true);
        assert.strictEqual(rec.fetches, 1);

        await featuresArrive(rec);

        assert.strictEqual(rec.wired, 1);
        assert.ok(rec.shown >= 1);
        click(rec, 'summary');
        assert.strictEqual(rec.sent.length, 1);
    });

    test(`a widget that starts on a bill still gets its buttons once [${target}]`, async () => {
        const { startFetch, rec } = loadWidget(target);
        rec.context = BILL;
        startFetch();
        await featuresArrive(rec);

        assert.strictEqual(rec.wired, 1);
        assert.ok(rec.shown >= 1);
        click(rec, 'summary');
        assert.strictEqual(rec.sent.length, 1);
    });

    test(`features that arrive after the visitor left the bill do not show the bar [${target}]`, async () => {
        const { widget, startFetch, rec } = loadWidget(target);
        rec.context = BILL;
        startFetch();
        widget.setPageContext(GENERAL, true);
        await featuresArrive(rec);

        assert.strictEqual(rec.shown, 0);
        assert.strictEqual(rec.wired, 0);
    });

    test(`with the feature off the bar is never shown and nothing is wired [${target}]`, async () => {
        const { widget, startFetch, rec } = loadWidget(target, { features: { quick_action_buttons_enabled: false } });
        startFetch();
        await featuresArrive(rec);
        widget.setPageContext(BILL, true);

        assert.strictEqual(rec.shown, 0);
        assert.strictEqual(rec.wired, 0);
    });

    test(`a failed features request is tried again at the next context change [${target}]`, async () => {
        const { widget, startFetch, rec } = loadWidget(target, { fetchFails: true });
        startFetch();
        await featuresArrive(rec);
        assert.strictEqual(rec.fetches, 1);

        widget.setPageContext(BILL, true);
        assert.strictEqual(rec.fetches, 2);
    });
}
