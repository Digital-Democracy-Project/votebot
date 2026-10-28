// Run with: npm test
//
// VOTEBOT-62 (step 3 of plans/PLAN-chat-widget-react-and-design-system.md): characterization tests of what the
// current widget does and no test covered: the visitor id and session storage (keys, 30-minute rule), the
// WebSocket reconnection and heartbeat, and the human-handoff states. They describe today's behaviour so that
// whatever replaces the widget can be held to it; they do not change it.
//
// websocket.js and chat.js are scripts with top-level `const`s, so each test loads them (src) or the whole
// committed bundle (dist) in a vm with a fake WebSocket, fake storage and fake timers.
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

const WS_URL = 'wss://example.test/ws/chat';
const MINUTE = 60 * 1000;

function makeStorage(initial = {}, { throws = false } = {}) {
    const map = new Map(Object.entries(initial));
    const fail = () => {
        throw new Error('storage disabled');
    };
    return {
        getItem: (key) => (throws ? fail() : map.has(key) ? map.get(key) : null),
        setItem: (key, value) => (throws ? fail() : void map.set(key, String(value))),
        removeItem: (key) => (throws ? fail() : void map.delete(key)),
        has: (key) => map.has(key),
        keys: () => [...map.keys()],
    };
}

// One page load. Storage can be handed over to the next load to model a navigation or a new visit.
function load(target, { local = makeStorage(), session = makeStorage() } = {}) {
    const sockets = [];
    class FakeWebSocket {
        constructor(url) {
            this.url = url;
            this.readyState = 0;
            this.sent = [];
            sockets.push(this);
        }
        send(data) {
            this.sent.push(JSON.parse(data));
        }
        close() {
            this.readyState = 3;
        }
    }
    FakeWebSocket.OPEN = 1;

    const timeouts = [];
    const intervals = [];
    const clearedIntervals = [];
    const window = { location: { href: 'https://example.test/' }, innerHeight: 800, pageYOffset: 0 };
    window.window = window;
    const context = vm.createContext({
        window,
        document: { readyState: 'loading', addEventListener() {}, referrer: '', documentElement: {}, body: {} },
        console: { log() {}, warn() {}, error() {} },
        URL,
        URLSearchParams,
        Date,
        Math,
        JSON,
        WebSocket: FakeWebSocket,
        localStorage: local,
        sessionStorage: session,
        setTimeout: (fn, ms) => timeouts.push({ fn, ms }),
        setInterval: (fn, ms) => intervals.push({ fn, ms }),
        clearInterval: (id) => clearedIntervals.push(id),
    });
    vm.runInContext(TARGETS[target]() + '\nwindow.__t = { DDPChat, DDPWebSocket };', context);
    const { DDPChat, DDPWebSocket } = window.__t;
    return { DDPChat, DDPWebSocket, sockets, timeouts, intervals, clearedIntervals, local, session };
}

// objects made inside the vm have another realm's prototypes: compare plain copies
const plain = (value) => JSON.parse(JSON.stringify(value));
const open = (socket) => {
    socket.readyState = 1;
    socket.onopen();
};
const close = (socket) => {
    socket.readyState = 3;
    socket.onclose({ code: 1006, reason: '' });
};
const serverSays = (socket, message) => socket.onmessage({ data: JSON.stringify(message) });
const connect = (h) => {
    const received = [];
    const statuses = [];
    h.DDPWebSocket.connect(WS_URL, (m) => received.push(m), (s) => statuses.push(s));
    return { received, statuses };
};

for (const target of Object.keys(TARGETS)) {
    // --- the visitor id (localStorage, survives sessions) ---------------------------------------------------

    test(`the visitor id is made once, kept in localStorage and reused on the next visit [${target}]`, () => {
        const first = load(target);
        const id = first.DDPWebSocket.getVisitorId();
        assert.match(id, /^v_[0-9a-f]{1,12}$/);
        assert.strictEqual(first.local.getItem('ddp_votebot_visitor_id'), id);
        assert.strictEqual(first.DDPWebSocket.getVisitorId(), id);

        const nextVisit = load(target, { local: first.local, session: makeStorage() });
        assert.strictEqual(nextVisit.DDPWebSocket.getVisitorId(), id);
    });

    test(`with storage disabled the visitor id is null and nothing throws [${target}]`, () => {
        const h = load(target, { local: makeStorage({}, { throws: true }), session: makeStorage({}, { throws: true }) });
        assert.strictEqual(h.DDPWebSocket.getVisitorId(), null);
        h.DDPWebSocket.storageSet('popup_open', '1'); // does not throw
        assert.strictEqual(h.DDPWebSocket.storageGet('popup_open'), null);
    });

    // --- the session (sessionStorage, 30 minutes) -----------------------------------------------------------

    test(`session_info stores the session id and the time under the ddp_votebot_ keys, and the next connect reuses it [${target}]`, () => {
        const h = load(target);
        const { received } = connect(h);
        assert.strictEqual(h.sockets[0].url, WS_URL); // no session yet
        open(h.sockets[0]);
        serverSays(h.sockets[0], { type: 'session_info', payload: { session_id: 'abc123' } });

        assert.strictEqual(h.session.getItem('ddp_votebot_session_id'), 'abc123');
        assert.match(h.session.getItem('ddp_votebot_last_activity'), /^\d+$/);
        assert.strictEqual(h.DDPWebSocket.getSessionId(), 'abc123');
        assert.strictEqual(received[0].type, 'session_info'); // and the message still reaches the chat

        const navigated = load(target, { session: h.session });
        connect(navigated);
        assert.strictEqual(navigated.sockets[0].url, `${WS_URL}?session_id=abc123`);
    });

    test(`a session idle for 29 minutes is kept, for 31 it is dropped with its page context and popup state [${target}]`, () => {
        const stored = (ageMinutes) =>
            makeStorage({
                ddp_votebot_session_id: 'abc123',
                ddp_votebot_page_context: '{"type":"bill"}',
                ddp_votebot_popup_open: '1',
                ddp_votebot_last_activity: String(Date.now() - ageMinutes * MINUTE),
            });

        const recent = load(target, { session: stored(29) });
        connect(recent);
        assert.strictEqual(recent.sockets[0].url, `${WS_URL}?session_id=abc123`);

        const stale = load(target, { session: stored(31) });
        connect(stale);
        assert.strictEqual(stale.sockets[0].url, WS_URL);
        assert.deepStrictEqual(stale.session.keys(), []); // session_id, last_activity, page_context, popup_open all gone
    });

    test(`a stored session with no recorded activity counts as expired [${target}]`, () => {
        const h = load(target, { session: makeStorage({ ddp_votebot_session_id: 'abc123' }) });
        connect(h);
        assert.strictEqual(h.sockets[0].url, WS_URL);
    });

    test(`sending is refused until the socket is open, and a sent message counts as activity [${target}]`, () => {
        const h = load(target);
        connect(h);
        assert.strictEqual(h.DDPWebSocket.send({ type: 'x' }), false);
        assert.strictEqual(h.DDPWebSocket.isConnected(), false);
        open(h.sockets[0]);
        assert.strictEqual(h.DDPWebSocket.send({ type: 'user_message' }), true);
        assert.deepStrictEqual(h.sockets[0].sent, [{ type: 'user_message' }]);
        assert.ok(h.session.has('ddp_votebot_last_activity'));
    });

    // --- reconnection and heartbeat -------------------------------------------------------------------------

    test(`a closed socket is retried after 1, 2, 4 and 8 seconds, then the visitor is told to refresh [${target}]`, () => {
        const h = load(target);
        const { received, statuses } = connect(h);
        open(h.sockets[0]);

        const delays = [];
        let socket = h.sockets[0];
        for (let attempt = 0; attempt < 4; attempt++) {
            close(socket);
            const retry = h.timeouts[h.timeouts.length - 1];
            delays.push(retry.ms);
            retry.fn();
            socket = h.sockets[h.sockets.length - 1];
        }
        assert.deepStrictEqual(delays, [1000, 2000, 4000, 8000]);
        assert.strictEqual(h.sockets.length, 5);
        assert.ok(statuses.includes('disconnected') && statuses.includes('connecting') && statuses.includes('connected'));

        const timersBefore = h.timeouts.length;
        close(socket); // the fifth close: no more tries
        assert.strictEqual(h.timeouts.length, timersBefore);
        assert.deepStrictEqual(plain(received[received.length - 1]), {
            type: 'connection_failed',
            payload: { message: 'Connection lost. Please refresh the page.' },
        });
    });

    test(`a connection that opens starts the count again, and each retry asks for the same session [${target}]`, () => {
        const h = load(target, { session: makeStorage({ ddp_votebot_session_id: 'abc123', ddp_votebot_last_activity: String(Date.now()) }) });
        connect(h);
        close(h.sockets[0]);
        h.timeouts[0].fn(); // 1 s
        close(h.sockets[1]);
        assert.strictEqual(h.timeouts[1].ms, 2000);
        h.timeouts[1].fn();
        assert.strictEqual(h.sockets[2].url, `${WS_URL}?session_id=abc123`);

        open(h.sockets[2]); // connected: the count starts again
        close(h.sockets[2]);
        assert.strictEqual(h.timeouts[2].ms, 1000);
    });

    test(`a ping goes out every 30 seconds while the socket is open, and the timer stops when it closes [${target}]`, () => {
        const h = load(target);
        connect(h);
        open(h.sockets[0]);
        assert.strictEqual(h.intervals.length, 1);
        assert.strictEqual(h.intervals[0].ms, 30000);

        h.intervals[0].fn();
        assert.deepStrictEqual(h.sockets[0].sent, [{ type: 'ping' }]);
        h.sockets[0].readyState = 2; // closing: no ping into a closing socket
        h.intervals[0].fn();
        assert.strictEqual(h.sockets[0].sent.length, 1);

        close(h.sockets[0]);
        assert.strictEqual(h.clearedIntervals.length, 1);
    });

    // --- human handoff (chat.js) ----------------------------------------------------------------------------

    // as widget.js wires it: what the socket receives goes to DDPChat.handleServerMessage
    function chatWithUI(h) {
        const ui = [];
        h.DDPChat.init((message) => ui.push(message));
        h.DDPWebSocket.connect(WS_URL, h.DDPChat.handleServerMessage, () => {});
        open(h.sockets[0]);
        return ui;
    }
    const types = (ui) => ui.map((m) => m.type);

    test(`an agent joining, speaking and leaving drives the handoff state and the UI [${target}]`, () => {
        const h = load(target);
        const ui = chatWithUI(h);
        assert.strictEqual(h.DDPChat.getHandoffActive(), false);

        serverSays(h.sockets[0], { type: 'agent_joined', payload: { agent_name: 'Sam' } });
        assert.strictEqual(h.DDPChat.getHandoffActive(), true);
        assert.deepStrictEqual(plain(ui[ui.length - 1]), { type: 'agent_joined', payload: { agent_name: 'Sam' } });

        serverSays(h.sockets[0], { type: 'agent_message', payload: { text: 'Hello', agent_name: 'Sam' } });
        assert.deepStrictEqual(plain(ui[ui.length - 1]), { type: 'agent_message', payload: { text: 'Hello', agent_name: 'Sam' } });

        serverSays(h.sockets[0], { type: 'agent_left', payload: {} });
        assert.strictEqual(h.DDPChat.getHandoffActive(), false);
        assert.strictEqual(ui[ui.length - 1].type, 'agent_left');
    });

    test(`during a handoff a message is relayed without disabling the input or waiting for a bot answer [${target}]`, () => {
        const h = load(target);
        const ui = chatWithUI(h);
        serverSays(h.sockets[0], { type: 'agent_joined', payload: { agent_name: 'Sam' } });
        ui.length = 0;

        assert.strictEqual(h.DDPChat.sendMessage('Are you there?', null), true);
        assert.deepStrictEqual(types(ui), ['user_message']);
        assert.strictEqual(h.DDPChat.getIsStreaming(), false);
        assert.strictEqual(h.DDPChat.sendMessage('Hello?', null), true); // and another can follow at once
    });

    test(`outside a handoff a message disables the input and blocks the next one until the answer ends [${target}]`, () => {
        const h = load(target);
        const ui = chatWithUI(h);

        assert.strictEqual(h.DDPChat.sendMessage('What does this bill do?', null), true);
        assert.deepStrictEqual(types(ui), ['user_message', 'input_disable']);
        assert.strictEqual(h.DDPChat.getIsStreaming(), true);
        assert.strictEqual(h.DDPChat.sendMessage('Another?', null), false);

        serverSays(h.sockets[0], { type: 'stream_end', payload: { requires_human: false } });
        assert.strictEqual(h.DDPChat.getIsStreaming(), false);
        assert.ok(types(ui).includes('input_enable'));
        assert.ok(!types(ui).includes('handoff_suggested'));
    });

    test(`an answer that asks for a human suggests the handoff, and an agent leaving also frees a stuck stream [${target}]`, () => {
        const h = load(target);
        const ui = chatWithUI(h);
        h.DDPChat.sendMessage('I want to talk to a person', null);
        serverSays(h.sockets[0], { type: 'stream_end', payload: { requires_human: true } });
        assert.deepStrictEqual(types(ui).slice(-3), ['stream_end', 'input_enable', 'handoff_suggested']);

        h.DDPChat.sendMessage('Hello?', null); // streaming again
        assert.strictEqual(h.DDPChat.getIsStreaming(), true);
        serverSays(h.sockets[0], { type: 'agent_left', payload: {} });
        assert.strictEqual(h.DDPChat.getIsStreaming(), false);
    });

    test(`a server error frees the input [${target}]`, () => {
        const h = load(target);
        const ui = chatWithUI(h);
        h.DDPChat.sendMessage('Hello', null);
        serverSays(h.sockets[0], { type: 'error', payload: { code: 'processing_error', message: 'Sorry' } });
        assert.strictEqual(h.DDPChat.getIsStreaming(), false);
        assert.deepStrictEqual(types(ui).slice(-2), ['error', 'input_enable']);
    });
}
