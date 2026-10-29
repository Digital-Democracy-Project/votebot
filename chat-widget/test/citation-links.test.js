// Run with: npm test
//
// VOTEBOT-67: the "Sources" list under an answer made a link of whatever address the server sent (`link.href = citation.url`).
// The server builds a citation's url from text the model writes, so it can be any scheme (javascript:, data:, ...).
// The message text already goes through safeHref (http, https and mailto only: markdown-links.test.js); so must a source.
//
// ui.js keeps its module in an IIFE, so the test loads the source in a vm, exposes DDPUI, gives it a small fake DOM and
// finishes a streamed answer with citations (against both src/ui.js and the committed dist/ddp-chat.min.js, so a stale
// rebuild fails here).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const TARGETS = { src: ['src', 'ui.js'], dist: ['dist', 'ddp-chat.min.js'] };
const DECLARATION = 'const DDPUI = (function() {';

class FakeElement {
    constructor(tag) {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.parentElement = null;
        this.style = {};
        this.className = '';
        this.textContent = '';
        this.innerHTML = '';
    }
    appendChild(child) {
        child.parentElement = this;
        this.children.push(child);
        return child;
    }
    remove() {}
    addEventListener() {}
    all() {
        return this.children.flatMap((child) => [child, ...child.all()]);
    }
}

function loadUI(target) {
    const source = fs.readFileSync(path.join(__dirname, '..', ...TARGETS[target]), 'utf8');
    assert.ok(source.includes(DECLARATION), 'the DDPUI declaration moved: update this test');
    const messages = new FakeElement('div');
    messages.scrollHeight = messages.scrollTop = messages.clientHeight = 0;
    const root = {
        querySelector: (selector) => (selector === '.ddp-chat-messages' ? messages : new FakeElement('div')),
        querySelectorAll: () => [],
        getElementById: () => null,
    };
    const window = { addEventListener() {}, location: { search: '', href: 'https://example.test/', pathname: '/', hostname: 'example.test' } };
    window.window = window;
    const context = vm.createContext({
        window,
        document: { readyState: 'loading', addEventListener() {}, createElement: (tag) => new FakeElement(tag), head: { appendChild() {} } },
        console: { log() {}, warn() {}, error() {} },
        URLSearchParams,
    });
    vm.runInContext(source.replace(DECLARATION, 'const DDPUI = window.__ui = (function() {'), context);
    const ui = window.__ui;
    ui.init(root);
    ui.cacheElements();
    return { ui, messages };
}

// Streams one answer and finishes it with these citations; returns the elements the Sources list made.
function sourcesFor(target, citations) {
    const { ui, messages } = loadUI(target);
    ui.appendToStreamingMessage('An answer.');
    ui.finalizeStreamingMessage({ citations });
    const all = messages.all();
    return { links: all.filter((el) => el.tagName === 'A'), entries: all.filter((el) => el.className === 'ddp-citation') };
}

for (const target of Object.keys(TARGETS)) {
    test(`${target}: a source with an http(s) or mailto address is a link`, () => {
        const { links } = sourcesFor(target, [
            { source: 'Bill text', url: 'https://dev.digitaldemocracyproject.org/bill/FL/2026E/HB%205601E' },
            { source: 'Plain http', url: 'http://example.test/page' },
            { source: 'Mail', url: 'mailto:info@example.test' },
        ]);
        assert.deepStrictEqual(
            links.map((a) => a.href),
            ['https://dev.digitaldemocracyproject.org/bill/FL/2026E/HB%205601E', 'http://example.test/page', 'mailto:info@example.test']
        );
        for (const a of links) {
            assert.strictEqual(a.target, '_blank');
            assert.strictEqual(a.rel, 'noopener noreferrer');
        }
    });

    test(`${target}: an accepted address is used exactly as sent (an apostrophe is not rewritten)`, () => {
        const url = "https://example.test/o'brien/bill?q=it's";
        const { links } = sourcesFor(target, [{ source: 'Apostrophe', url }]);
        assert.deepStrictEqual(
            links.map((a) => a.href),
            [url]
        );
    });

    test(`${target}: a source with any other scheme is plain text, not a link`, () => {
        const hostile = [
            'javascript:alert(1)',
            '  JaVaScRiPt:alert(1)',
            'data:text/html,<script>alert(1)</script>',
            'vbscript:msgbox(1)',
            'file:///etc/passwd',
            '//evil.test/relative-scheme',
            'ftp://example.test/file',
        ];
        const { links, entries } = sourcesFor(
            target,
            hostile.map((url, i) => ({ source: `Source ${i}`, url }))
        );
        assert.strictEqual(links.length, 0, `links made: ${links.map((a) => a.href)}`);
        assert.deepStrictEqual(
            entries.map((el) => el.textContent),
            hostile.slice(0, 3).map((_, i) => `Source ${i}`) // only the first three citations are shown
        );
    });

    test(`${target}: a source address that is not a string cannot break the answer`, () => {
        const { links, entries } = sourcesFor(target, [
            { source: 'A number', url: 42 },
            { source: 'An object', url: { href: 'javascript:alert(1)' } },
            { source: 'Fine', url: 'https://example.test/ok' },
        ]);
        assert.deepStrictEqual(
            links.map((a) => a.href),
            ['https://example.test/ok']
        );
        assert.strictEqual(entries.length, 3);
    });

    test(`${target}: a source with no address, or an empty one, shows its name`, () => {
        const { links, entries } = sourcesFor(target, [{ source: 'No url' }, { document_id: 'doc-7', url: '' }]);
        assert.strictEqual(links.length, 0);
        assert.deepStrictEqual(
            entries.map((el) => el.textContent),
            ['No url', 'doc-7']
        );
    });
}
