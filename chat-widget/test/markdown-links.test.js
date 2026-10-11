// Run with: npm test
//
// The renderer escapes & < > but not quotes, and used to make a link of any [text](url) and to let a quote into an href.
// ui.js keeps its parser inside an IIFE, so the test loads the source in a vm and rewrites the declaration to expose it
// (against both src/ui.js and the committed dist/ddp-chat.min.js, so a stale rebuild fails here).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const TARGETS = { src: ['src', 'ui.js'], dist: ['dist', 'ddp-chat.min.js'] };
const DECLARATION = 'const markdownParser = {';

function loadParser(target) {
    const source = fs.readFileSync(path.join(__dirname, '..', ...TARGETS[target]), 'utf8');
    assert.ok(source.includes(DECLARATION), 'the parser declaration moved: update this test');
    const window = { location: { search: '', href: 'https://example.test/', pathname: '/', hostname: 'example.test' } };
    window.window = window;
    const context = vm.createContext({
        window,
        document: { readyState: 'loading', addEventListener() {}, createElement: () => ({ style: {} }), head: { appendChild() {} } },
        console: { log() {}, warn() {}, error() {} },
        URLSearchParams,
    });
    vm.runInContext(source.replace(DECLARATION, 'const markdownParser = window.__mp = {'), context);
    return window.__mp;
}

const LINK_TAG = /<a href="[^"]*" target="_blank" rel="noopener noreferrer">/g; // the only shape of anchor the renderer may emit

for (const target of Object.keys(TARGETS)) {
    test(`ordinary links render as before [${target}]`, () => {
        const { parse } = loadParser(target);
        assert.strictEqual(
            parse('See [the bill](https://example.test/explore/FL/2026/HB%20219?a=1&b=2) now'),
            '<p>See <a href="https://example.test/explore/FL/2026/HB%20219?a=1&amp;b=2" target="_blank" rel="noopener noreferrer">the bill</a> now</p>'
        );
        assert.match(parse('write [us](mailto:info@example.test)'), /<a href="mailto:info@example\.test"/);
        assert.match(parse('visit http://example.test/x today'), /<a href="http:\/\/example\.test\/x"[^>]*>http:\/\/example\.test\/x<\/a>/);
        assert.match(parse('[padded]( https://example.test/ok )'), /<a href="https:\/\/example\.test\/ok"/);
    });

    test(`a link whose URL is not http(s) or mailto stays text [${target}]`, () => {
        const { parse } = loadParser(target);
        for (const url of ['javascript:alert(1)', 'JaVaScRiPt:alert(1)', ' javascript:alert(1)', 'data:text/html,hi', 'vbscript:x', '//evil.test/x', '/relative']) {
            const html = parse(`[click](${url})`);
            assert.ok(!html.includes('<a '), `${url} became a link: ${html}`);
            assert.ok(html.includes('click'));
        }
    });

    test(`a quote cannot end the href, in a markdown link or a bare URL [${target}]`, () => {
        const { parse } = loadParser(target);
        for (const text of [
            '[x](https://a.test/"onmouseover="alert(1))',
            'see https://a.test/x"onmouseover="alert(1) now',
            "[x](https://a.test/'onmouseover='alert(1))",
            "see https://a.test/x'onmouseover='alert(1) now",
        ]) {
            const html = parse(text);
            const tags = html.match(/<a [^>]*>/g) || [];
            assert.strictEqual(tags.length, 1, html);
            assert.strictEqual((tags[0].match(LINK_TAG) || []).length, 1, `anchor has another shape: ${tags[0]}`);
            assert.ok(!/<a [^>]*\sonmouseover=/.test(html), html);
            assert.ok(/%22|%27/.test(tags[0]), tags[0]);
        }
    });

    test(`text, bold and a title with quotes are untouched [${target}]`, () => {
        const { parse } = loadParser(target);
        assert.strictEqual(parse('**The "Fair" Act** isn\'t a link'), '<p><strong>The "Fair" Act</strong> isn\'t a link</p>');
    });
}
