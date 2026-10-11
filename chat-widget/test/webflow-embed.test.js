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

function load(target, config = WEBFLOW_CONFIG) {
    const source = TARGETS[target]()
        .replace('const markdownParser = {', 'const markdownParser = window.__mp = {')
        .replace(/\}\)\(\);\s*$/, 'window.__resolve = resolvePageContext;\nwindow.__quick = handleQuickActionClick;\n})();');
    const window = {
        DDPChatConfig: { wsUrl: 'wss://example.test/ws/chat', pageContext: JSON.parse(JSON.stringify(config)) },
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
    return { window, context };
}

for (const target of Object.keys(TARGETS)) {
    test(`the Webflow embed's escaped title reaches the visitor as text, in the welcome message and the question [${target}]`, () => {
        const { window } = load(target);
        const ctx = window.__resolve();
        assert.strictEqual(ctx.title, "Making America's Food Safer Act (S 4974)");
        assert.strictEqual(ctx.id, 'S4974');
        assert.strictEqual(ctx.slug, 'making-americas-food-safer-act-s4974-2026');

        const welcome = window.DDPChatWidget.generateWelcomeMessage(ctx);
        assert.strictEqual(welcome, "Welcome! I can answer detailed questions about **Making America's Food Safer Act (S 4974)**.");
        const html = window.__mp.parse(welcome);
        assert.ok(html.includes("<strong>Making America's Food Safer Act (S 4974)</strong>"), html);
        assert.ok(!html.includes('&#39;') && !html.includes('&amp;#39;'), html);

    });
}

for (const target of Object.keys(TARGETS)) {
    test(`the Status & votes button asks about the bill by its decoded title, in the message actually sent [${target}]`, () => {
        const { window, context } = load(target);
        window.__ctx = window.__resolve();
        // the real DDPChat module, with only the network send replaced
        vm.runInContext(
            'DDPWebSocket.isConnected = function() { return true; };' +
            'DDPWebSocket.send = function(message) { window.__sent = message; return true; };' +
            'DDPChat.init(null, window.__ctx);',
            context
        );
        window.__quick('status_votes');
        const { type, payload } = window.__sent;
        assert.strictEqual(type, 'user_message');
        assert.strictEqual(payload.message, "What is the latest status and vote history for Making America's Food Safer Act (S 4974)?");
        assert.strictEqual(payload.button, 'status_votes');
        assert.strictEqual(payload.page_context.title, "Making America's Food Safer Act (S 4974)");
        assert.strictEqual(payload.page_context.slug, 'making-americas-food-safer-act-s4974-2026');
    });
}

const ALLOWED_TAGS = new Set(['p', 'a', 'strong', 'em', 'code', 'ul', 'li']); // no image, script or anything else; an anchor only of the strict shape below

for (const target of Object.keys(TARGETS)) {
    test(`an encoded-markup title never becomes an element, an event handler or a non-http link [${target}]`, () => {
        for (const title of [
            '&lt;img src=x onerror=alert(1)&gt;',
            '&lt;script&gt;alert(1)&lt;/script&gt;',
            '&lt;a href=&quot;https://evil.example&quot;&gt;x&lt;/a&gt;',
            '&#91;x&#93;&#40;javascript:alert(1)&#41;',
            '&#91;x&#93;&#40;javascript&amp;colon;alert(1)&#41;', // decodes to [x](javascript&colon;alert(1)): the & is escaped before the scheme is checked
            '&#91;x&#93;&#40;&amp;#106;avascript:alert(1)&#41;',
            '&#91;x&#93;&#40;https://evil.example/&quot;onmouseover=&quot;alert(1)&#41;',
            'https://evil.example/&quot;onmouseover=&quot;alert(1)',
        ]) {
            // (a title that decodes to markdown link syntax with an https URL does render as that link, as a literal title
            // always has; what must never happen is any other element, an event attribute, or a javascript: link)
            const { window } = load(target, { ...WEBFLOW_CONFIG, title });
            const welcome = window.DDPChatWidget.generateWelcomeMessage(window.__resolve());
            const html = window.__mp.parse(welcome);
            for (const [, tag] of html.matchAll(/<\/?([a-z0-9]+)/gi)) assert.ok(ALLOWED_TAGS.has(tag.toLowerCase()), `<${tag}> from ${title}: ${html}`);
            for (const [tag] of html.matchAll(/<(?!a[ >])[a-z0-9]+[^>]*>/gi)) assert.match(tag, /^<[a-z0-9]+>$/i, `a tag with attributes: ${tag}`);
            for (const anchor of html.match(/<a [^>]*>/g) || []) {
                assert.match(anchor, /^<a href="https?:[^"]*" target="_blank" rel="noopener noreferrer">$/, `${title}: ${anchor}`);
                assert.ok(!/\son[a-z]+=/i.test(anchor), anchor);
            }
        }
    });

    test(`a title escaped twice loses exactly one level in the widget, and the resolver runs once [${target}]`, () => {
        const { window } = load(target, { ...WEBFLOW_CONFIG, title: 'Tom &amp;amp; Jerry &amp;lt;b&amp;gt;' });
        assert.strictEqual(window.__resolve().title, 'Tom &amp; Jerry &lt;b&gt;'); // one pass: the widget never decodes its own output
    });
}
