// Run with: npm test
//
// VOTEBOT-63: no full page address leaves the widget. A page address can carry a sign-in token, an
// email-link token or personal data in its query string or fragment, or credentials before the host, so
// what is sent (`page_url`, and the `url` inside `page_context`) is origin and path only.
//
// chat.js is a script with a top-level `const DDPChat`, so each test loads the source in a vm with a
// stubbed window/document and reads DDPChat back out. Every test runs against src (websocket.js + chat.js)
// and the committed dist/ddp-chat.min.js (what ships), so a stale rebuild fails here.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const read = (...parts) => fs.readFileSync(path.join(__dirname, '..', ...parts), 'utf8');
const TARGETS = {
    src: () => read('src', 'websocket.js') + '\n' + read('src', 'chat.js'),
    dist: () => read('dist', 'ddp-chat.min.js'),
};

const FULL = 'https://user:secret@example.test:8443/explore/FL/2026/HB%20219?token=abc123&utm_source=x#access_token=zzz';
const SAFE = 'https://example.test:8443/explore/FL/2026/HB%20219';

// Loads the target with `href` as the page's address; returns the chat and what it sent on the socket.
function load(target, href) {
    const window = { location: { href }, innerHeight: 800, pageYOffset: 0 };
    window.window = window;
    const context = vm.createContext({
        window,
        document: { readyState: 'loading', addEventListener() {}, referrer: '', documentElement: {}, body: {} },
        console: { log() {}, warn() {}, error() {} },
        URL,
        Date,
        Math,
    });
    vm.runInContext(TARGETS[target]() + '\nwindow.__t = { DDPChat, DDPWebSocket };', context);
    const { DDPChat, DDPWebSocket } = window.__t;
    const sent = [];
    DDPWebSocket.isConnected = () => true;
    DDPWebSocket.getVisitorId = () => 'v_test';
    DDPWebSocket.send = (message) => {
        sent.push(message);
        return true;
    };
    return { DDPChat, sent };
}

for (const target of Object.keys(TARGETS)) {
    test(`a message is sent with the page address as origin and path only [${target}]`, () => {
        const { DDPChat, sent } = load(target, FULL);
        DDPChat.sendMessage('What does this bill do?', null);
        assert.strictEqual(sent.length, 1);
        assert.strictEqual(sent[0].payload.page_url, SAFE);
    });

    test(`the url inside the page context is reduced too, whichever mode set it [${target}]`, () => {
        const { DDPChat, sent } = load(target, FULL);
        // auto-detect and ?ddp_type= set the page's own address; /content/resolve echoes the ?ddp_url= value
        DDPChat.setPageContext({ type: 'bill', id: 'HB 219', url: FULL });
        DDPChat.sendMessage('Summary?', 'summary');
        const context = sent[0].payload.page_context;
        assert.strictEqual(context.url, SAFE);
        assert.strictEqual(context.id, 'HB 219'); // everything else is as it was
        assert.strictEqual(DDPChat.getPageContext().url, FULL); // the widget's own copy is not changed
    });

    test(`the context_update sent on a restored session carries the reduced url [${target}]`, () => {
        const { DDPChat } = load(target, FULL);
        const sentContext = DDPChat.contextForServer({ type: 'bill', url: FULL, title: 'T' });
        assert.deepStrictEqual(JSON.parse(JSON.stringify(sentContext)), { type: 'bill', url: SAFE, title: 'T' });
    });

    test(`a value that is not an http(s) address is left out, not sent [${target}]`, () => {
        const { DDPChat, sent } = load(target, 'javascript:alert(1)//?token=abc');
        DDPChat.setPageContext({ type: 'general', url: 'not a url?token=abc' });
        DDPChat.sendMessage('Hello', null);
        assert.strictEqual(sent[0].payload.page_url, null);
        assert.ok(!('url' in sent[0].payload.page_context));
    });

    test(`the Voatz page: the address is the page, the bill link in ?ddp_url= is not sent on [${target}]`, () => {
        const href = 'https://votebot.digitaldemocracyproject.org/?ddp_url=https://digitaldemocracyproject.org/bills/some-bill-2026';
        const { DDPChat, sent } = load(target, href);
        DDPChat.sendMessage('Hello', null);
        assert.strictEqual(sent[0].payload.page_url, 'https://votebot.digitaldemocracyproject.org/');
    });

    test(`a context with no url is passed through untouched [${target}]`, () => {
        const { DDPChat } = load(target, FULL);
        const context = { type: 'general' };
        assert.strictEqual(DDPChat.contextForServer(context), context);
    });
}
