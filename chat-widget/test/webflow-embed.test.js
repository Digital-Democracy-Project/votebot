// Run with: npm test
//
// The real cause of VOTEBOT-1: the Webflow bill-page embed fills the widget's config from CMS merge fields, and Webflow
// HTML-escapes them ("America's" -> "America&#39;s") although the embed is a <script>, where the browser decodes nothing.
// This is that embed's config, end to end: config -> resolved page context -> the welcome message -> the rendered HTML.
// The whole bundle is loaded the way build.js joins it (src) or as shipped (dist), with the parser and the page-context
// resolver exposed by rewriting two declarations.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
const BUILD_ORDER = ['websocket.js', 'chat.js', 'ui.js', 'widget.js']; // as in build.js (JS_FILES)
const TARGETS = {
    src: () => BUILD_ORDER.map(f => fs.readFileSync(path.join(ROOT, 'src', f), 'utf8')).join('\n'),
    dist: () => fs.readFileSync(path.join(ROOT, 'dist', 'ddp-chat.min.js'), 'utf8'),
};

// What the live Webflow bill page for S 4974 sends (taken from its HTML, 2026-10-11)
const WEBFLOW_CONFIG = {
    type: 'bill',
    title: 'Making America&#39;s Food Safer Act (S 4974)',
    slug: 'making-americas-food-safer-act-s4974-2026',
    jurisdictionIso2: 'US',
    sessionCode: '119',
    billId: 'S4974',
};

function load(target) {
    const source = TARGETS[target]()
        .replace('const markdownParser = {', 'const markdownParser = window.__mp = {')
        .replace(/\}\)\(\);\s*$/, 'window.__resolve = resolvePageContext;\n})();');
    const window = {
        DDPChatConfig: { wsUrl: 'wss://example.test/ws/chat', pageContext: JSON.parse(JSON.stringify(WEBFLOW_CONFIG)) },
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
    return window;
}

for (const target of Object.keys(TARGETS)) {
    test(`the Webflow embed's escaped title reaches the visitor as text, in the welcome message and the question [${target}]`, () => {
        const window = load(target);
        const ctx = window.__resolve();
        assert.strictEqual(ctx.title, "Making America's Food Safer Act (S 4974)");
        assert.strictEqual(ctx.id, 'S4974');
        assert.strictEqual(ctx.slug, 'making-americas-food-safer-act-s4974-2026');

        const welcome = window.DDPChatWidget.generateWelcomeMessage(ctx);
        assert.strictEqual(welcome, "Welcome! I can answer detailed questions about **Making America's Food Safer Act (S 4974)**.");
        const html = window.__mp.parse(welcome);
        assert.ok(html.includes("<strong>Making America's Food Safer Act (S 4974)</strong>"), html);
        assert.ok(!html.includes('&#39;') && !html.includes('&amp;#39;'), html);

        // the Status & votes button sends 'What is the latest status and vote history for <title or id>?'
        const question = 'What is the latest status and vote history for ' + (ctx.title || ctx.id) + '?';
        assert.strictEqual(question, "What is the latest status and vote history for Making America's Food Safer Act (S 4974)?");
    });
}
