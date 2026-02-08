# DDP Chat Widget

Embeddable chat widget for Digital Democracy Project VoteBot.

## Live Demo

**Hosted version:** https://votebot.digitaldemocracyproject.org/

**With page context:** https://votebot.digitaldemocracyproject.org/?ddp_url=https://digitaldemocracyproject.org/bills/one-big-beautiful-bill-act-hr1-2025

## Features

- Single JavaScript file (~57KB minified)
- Shadow DOM for style isolation
- WebSocket streaming with auto-reconnection
- Markdown rendering
- **Personalized welcome messages** based on page context
- **Cross-page navigation persistence** — session, conversation history, and popup state survive full-page navigations within the same browser tab via `sessionStorage`
- **Context-change notifications** — when the user navigates to a different entity, conversation is restored and a notice like "You're now viewing **New Bill**" is shown
- **Auto-detect mode** for websites (detects bill/legislator from page)
- **Explicit context mode** for mobile apps
- **URL parameter support** for passing DDP URLs (`?ddp_url=...`)
- **Content resolution API** to fetch metadata from Webflow CMS
- **Smart auto-scroll** - pauses when user scrolls up; "scroll to bottom" button to resume
- **Bill info pre-fetching** - fetches bill details from OpenStates before streaming for bills not in RAG
- **Auto-open modes**:
  - Explicit mode (`?ddp_url=...`): Widget auto-opens when URL parameter provided
  - Discovery mode: Widget stays closed, auto-detects page context when opened
- Human agent handoff support via Slack
- **Full-screen mobile experience** on smaller screens (<480px) using `dvh` (dynamic viewport height) for correct sizing when the browser URL bar is visible
- Safe area support for notched phones (iPhone X+)

## Quick Start

### Building

```bash
npm install
npm run build
```

Output: `dist/ddp-chat.min.js`

### Local Testing

1. Start VoteBot server:
   ```bash
   cd ../
   python -m votebot.main
   ```

2. Serve the widget directory over HTTP (needed for WebSocket connections):
   ```bash
   cd chat-widget
   python3 -m http.server 8080
   ```

3. Open `http://localhost:8080/test.html` in a browser

The test page connects to `ws://localhost:8000/ws/chat` and includes navigation links to `test-page2.html` and `test-page3.html` for testing cross-page session persistence.

## Page Context Modes

The widget supports two modes for providing page context:

### Mode 1: Mobile App / Explicit Context

Pass the context explicitly when you know what content the user is viewing:

```html
<script>
    window.DDPChatConfig = {
        wsUrl: 'wss://api.digitaldemocracyproject.org/ws/chat',
        pageContext: {
            type: 'bill',
            id: 'HR 1',
            title: 'One Big Beautiful Bill Act',
            jurisdiction: 'US'
        }
    };
</script>
<script src="https://api.digitaldemocracyproject.org/widget/ddp-chat.min.js" async></script>
```

**Welcome message:** "Welcome! I can answer detailed questions about **One Big Beautiful Bill Act (HR 1)**. You can also ask me about other bills, legislators, or Digital Democracy Project in general."

### Mode 2: Website Auto-Detection

Let the widget automatically detect context from the page:

```html
<script>
    window.DDPChatConfig = {
        wsUrl: 'wss://api.digitaldemocracyproject.org/ws/chat',
        autoDetect: true
    };
</script>
<script src="https://api.digitaldemocracyproject.org/widget/ddp-chat.min.js" async></script>
```

The widget detects context from (in order of priority):

1. **Data attributes** on any element:
   ```html
   <body data-ddp-type="bill"
         data-ddp-id="HR 1"
         data-ddp-title="One Big Beautiful Bill Act"
         data-ddp-jurisdiction="US">
   ```

2. **JSON-LD structured data**:
   ```html
   <script type="application/ld+json">
   {
     "@context": "https://schema.org",
     "@type": "Legislation",
     "name": "One Big Beautiful Bill Act",
     "identifier": "HR 1"
   }
   </script>
   ```

3. **URL patterns**: `/bill/HR-1`, `/legislator/john-smith`, `/organization/aclu`

4. **Meta tags**: `og:title`, `og:type`

### Mode 3: URL Parameter (for hosted version)

Pass a DDP URL as a query parameter to automatically resolve context:

```
https://votebot.digitaldemocracyproject.org/?ddp_url=https://digitaldemocracyproject.org/bills/one-big-beautiful-bill-act-hr1-2025
```

The widget calls the content resolution API to fetch metadata from Webflow CMS:

```bash
GET /votebot/v1/content/resolve?url=https://digitaldemocracyproject.org/bills/one-big-beautiful-bill-act-hr1-2025
```

**Supported URL patterns:**
- Bills: `/bills/{slug}`
- Legislators: `/legislators/{slug}`
- Organizations: `/member-organizations/{slug}`

## Session Persistence

The widget persists chat sessions across full-page navigations within the same browser tab using `sessionStorage`. This is critical for multi-page sites like Webflow where every link click triggers a full page reload.

### How It Works

- **Storage**: All state is stored in `sessionStorage` with the prefix `ddp_votebot_`. This is scoped to the browser tab and automatically cleared when the tab is closed.
- **Session ID**: When the WebSocket connects and receives a `session_info` response, the session ID is saved. On subsequent page loads, the saved session ID is sent to the server, which restores the conversation.
- **Activity timeout**: A 30-minute inactivity timeout ensures stale sessions are cleared. Each message send and server response resets the timer.
- **Page context**: The current page context is persisted so the widget can detect when the user navigates to a different entity.
- **Popup state**: Whether the chat popup is open or closed is persisted, so it stays open across navigations.

### Cross-Page Navigation Behavior

| Scenario | Behavior |
|----------|----------|
| **First visit** | Welcome message: "Welcome! I can answer questions about **Bill Title**." |
| **Navigate to different entity** | Fresh session with welcome message for the new entity (old conversation is discarded to avoid confusing the LLM) |
| **Same-page refresh** | Conversation history restored, no extra message |
| **Close tab + reopen** | Fresh session (`sessionStorage` cleared) with welcome message |
| **30-minute inactivity** | Session expires, next page load starts fresh |

### Storage Keys

| Key | Purpose |
|-----|---------|
| `ddp_votebot_session_id` | WebSocket session ID for reconnection |
| `ddp_votebot_last_activity` | Timestamp of last activity (for 30-min timeout) |
| `ddp_votebot_page_context` | JSON-serialized page context for change detection |
| `ddp_votebot_popup_open` | `"1"` or `"0"` — popup visibility state |

## Embedding on Your Website

Basic embedding (no context):

```html
<script>
    window.DDPChatConfig = {
        wsUrl: 'wss://api.digitaldemocracyproject.org/ws/chat'
    };
</script>
<script src="https://api.digitaldemocracyproject.org/widget/ddp-chat.min.js" async></script>
```

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `wsUrl` | string | `wss://api.digitaldemocracyproject.org/votebot/ws` | WebSocket server URL |
| `position` | string | `bottom-right` | Widget position |
| `primaryColor` | string | `#1a5f7a` | Primary brand color |
| `botName` | string | `VoteBot` | Bot display name |
| `avatar` | string | `🗳️` | Bot avatar emoji |
| `welcomeMessage` | string | `null` | Custom welcome message (null = auto-generate) |
| `pageContext` | object | `null` | Explicit page context (null = use autoDetect) |
| `autoDetect` | boolean | `false` | Auto-detect context from page |
| `autoOpen` | boolean | `false` | Automatically open the chat popup on page load |

### Page Context

Provide page context to help VoteBot give more relevant answers:

```javascript
pageContext: {
    type: 'bill' | 'legislator' | 'organization' | 'general',
    id: 'FL-HB-1234',
    title: 'Education Funding Act',
    jurisdiction: 'FL',
    url: 'https://example.com/bill/123'
}
```

## JavaScript API

Control the widget programmatically:

```javascript
// Open the chat popup
DDPChatWidget.open();

// Close the chat popup
DDPChatWidget.close();

// Toggle the chat popup
DDPChatWidget.toggle();

// Update page context (e.g., when user navigates in a SPA)
DDPChatWidget.setPageContext({ type: 'bill', id: 'HB-1234', title: 'My Bill' });

// Update page context AND show a new personalized welcome message
DDPChatWidget.setPageContext({ type: 'bill', id: 'HR 1', title: 'Big Bill' }, true);

// Get current page context
DDPChatWidget.getPageContext();

// Generate a welcome message for a given context (useful for previews)
DDPChatWidget.generateWelcomeMessage({ type: 'bill', title: 'My Bill', id: 'HB-1' });

// Check connection status
DDPChatWidget.isConnected();

// Get session ID
DDPChatWidget.getSessionId();
```

## Deployment

### Prerequisites

- VoteBot server running with WebSocket support
- Nginx for reverse proxy
- Domain configured in Cloudflare (or similar DNS)
- (Optional) Slack workspace configured for human handoff

### Server Setup

#### Step 1: Build the Widget

```bash
cd chat-widget
npm install
npm run build
```

#### Step 2: Deploy Widget to Server

```bash
# On the server, create the web directory
sudo mkdir -p /var/www/votebot

# Copy widget files
scp dist/ddp-chat.min.js your-server:/var/www/votebot/
scp index.html your-server:/var/www/votebot/
```

#### Step 3: Configure Nginx

**For the widget site (votebot.digitaldemocracyproject.org):**

```bash
sudo cp nginx.conf /etc/nginx/sites-available/votebot
sudo ln -s /etc/nginx/sites-available/votebot /etc/nginx/sites-enabled/
```

**For the API (api.digitaldemocracyproject.org), add these location blocks:**

```nginx
# VoteBot API endpoints
location /votebot/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

# WebSocket support for VoteBot
location /ws/chat {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_read_timeout 86400;
}
```

#### Step 4: Set Up SSL

```bash
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d votebot.digitaldemocracyproject.org
```

#### Step 5: Start VoteBot

```bash
cd ~/votebot
source venv/bin/activate
PYTHONPATH=src uvicorn votebot.main:app --host 127.0.0.1 --port 8000
```

Or run as a background service:

```bash
nohup sh -c 'PYTHONPATH=src uvicorn votebot.main:app --host 127.0.0.1 --port 8000' > /tmp/votebot.log 2>&1 &
```

### Production URLs

| Resource | URL |
|----------|-----|
| Widget landing page | https://votebot.digitaldemocracyproject.org/ |
| Widget with context | https://votebot.digitaldemocracyproject.org/?ddp_url={DDP_URL} |
| Widget JS file | https://api.digitaldemocracyproject.org/widget/ddp-chat.min.js |
| WebSocket endpoint | wss://api.digitaldemocracyproject.org/ws/chat |
| Content resolve API | https://api.digitaldemocracyproject.org/votebot/v1/content/resolve?url={URL} |

### Production Checklist

- [ ] Widget built with `npm run build`
- [ ] Widget files deployed to `/var/www/votebot/`
- [ ] Nginx configured for votebot subdomain
- [ ] Nginx configured to proxy `/votebot/` and `/ws/chat` to VoteBot
- [ ] SSL certificates installed via certbot
- [ ] DNS configured in Cloudflare (proxy enabled)
- [ ] VoteBot running on port 8000
- [ ] CORS configured to allow widget connections from embedding sites
- [ ] (Optional) Slack integration configured for human handoff

## Human Handoff

When users request human assistance, the widget supports seamless handoff to human agents via Slack. See the main [VoteBot README](../README.md) for Slack integration setup.

### User Experience

1. User sends message like "I want to talk to a human"
2. VoteBot detects handoff request and creates Slack thread
3. Widget shows "Connecting you with a human agent..." banner
4. Human agent replies in Slack thread
5. Agent messages appear in widget with agent's name
6. Agent reacts with ✅ to end handoff
7. Widget returns to VoteBot mode

## Architecture

```
src/
├── widget.js      # Entry point, Shadow DOM, session persistence, context-change detection
├── websocket.js   # WebSocket connection with auto-reconnect and sessionStorage layer
├── chat.js        # Message handling, streaming, handoff state
├── ui.js          # DOM manipulation, markdown rendering
└── styles.css     # Scoped styles for Shadow DOM

dist/
└── ddp-chat.min.js  # Built single file (JS + CSS inlined)
```

## Development

### File Structure

| File | Purpose |
|------|---------|
| `src/widget.js` | Entry point, reads config, creates Shadow DOM, session persistence logic |
| `src/websocket.js` | WebSocket with reconnection logic and `sessionStorage` persistence layer |
| `src/chat.js` | Message handling, streaming state |
| `src/ui.js` | DOM manipulation, markdown parser |
| `src/styles.css` | Widget styles |
| `build.js` | Build script (concat + minify) |
| `test.html` | Local testing page (bill context) |
| `test-page2.html` | Navigation test page (different bill) |
| `test-page3.html` | Navigation test page (legislator) |

### Build Process

The `build.js` script:
1. Reads CSS and inlines it as a JavaScript string
2. Concatenates all JS modules in dependency order
3. Minifies with terser
4. Outputs single file to `dist/ddp-chat.min.js`

### Testing Changes

1. Make changes to source files in `src/`
2. Run `npm run build` (or `npm run watch` for auto-rebuild)
3. Refresh `test.html` in browser
4. Test with VoteBot running locally
5. For session persistence testing, use the navigation links between `test.html`, `test-page2.html`, and `test-page3.html`
