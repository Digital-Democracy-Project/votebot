# DDP Chat Widget

Embeddable chat widget for Digital Democracy Project VoteBot.

## Features

- Single JavaScript file (~27KB minified)
- Shadow DOM for style isolation
- WebSocket streaming with auto-reconnection
- Markdown rendering
- **Personalized welcome messages** based on page context
- **Auto-detect mode** for websites (detects bill/legislator from page)
- **Explicit context mode** for mobile apps
- Human agent handoff support via Slack
- **Full-screen mobile experience** on smaller screens (<480px)
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

2. Open `test.html` in a browser

The test page is configured to connect to `ws://localhost:8000/ws/chat`.

## Page Context Modes

The widget supports two modes for providing page context:

### Mode 1: Mobile App / Explicit Context

Pass the context explicitly when you know what content the user is viewing:

```html
<script>
    window.DDPChatConfig = {
        wsUrl: 'wss://api.digitaldemocracyproject.org/votebot/ws',
        pageContext: {
            type: 'bill',
            id: 'HR-1',
            title: 'One Big Beautiful Bill Act',
            jurisdiction: 'US'
        }
    };
</script>
<script src="https://api.digitaldemocracyproject.org/widget/ddp-chat.min.js" async></script>
```

**Welcome message:** "Welcome! I can answer detailed questions about **One Big Beautiful Bill Act (HR-1)**. You can also ask me about other bills, legislators, or Digital Democracy Project in general."

### Mode 2: Website Auto-Detection

Let the widget automatically detect context from the page:

```html
<script>
    window.DDPChatConfig = {
        wsUrl: 'wss://api.digitaldemocracyproject.org/votebot/ws',
        autoDetect: true
    };
</script>
<script src="https://api.digitaldemocracyproject.org/widget/ddp-chat.min.js" async></script>
```

The widget detects context from (in order of priority):

1. **Data attributes** on any element:
   ```html
   <body data-ddp-type="bill"
         data-ddp-id="HR-1"
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
     "identifier": "HR-1"
   }
   </script>
   ```

3. **URL patterns**: `/bill/HR-1`, `/legislator/john-smith`, `/organization/aclu`

4. **Meta tags**: `og:title`, `og:type`

## Embedding on Your Website

Basic embedding (no context):

```html
<script>
    window.DDPChatConfig = {
        wsUrl: 'wss://api.digitaldemocracyproject.org/votebot/ws'
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
DDPChatWidget.setPageContext({ type: 'bill', id: 'HR-1', title: 'Big Bill' }, true);

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
- DDP-API server for hosting the widget file
- (Optional) Slack workspace configured for human handoff

### Step 1: Build the Widget

```bash
cd chat-widget
npm install
npm run build
```

### Step 2: Copy to DDP-API Static Directory

```bash
# Create the widget directory if it doesn't exist
mkdir -p /path/to/DDP-API/static/widget

# Copy the built widget
cp dist/ddp-chat.min.js /path/to/DDP-API/static/widget/
```

### Step 3: Configure DDP-API to Serve Static Files

Add to `/DDP-API/app/main.py`:

```python
from pathlib import Path
from fastapi.staticfiles import StaticFiles

# Mount widget static files
static_dir = Path(__file__).parent.parent / "static" / "widget"
if static_dir.exists():
    app.mount("/widget", StaticFiles(directory=str(static_dir)), name="widget")
```

### Step 4: Deploy

1. Deploy DDP-API with the widget file
2. Ensure VoteBot is running with WebSocket endpoint at `/ws/chat`
3. The widget will be available at:
   ```
   https://api.digitaldemocracyproject.org/widget/ddp-chat.min.js
   ```

### Production Checklist

- [ ] Widget built with `npm run build`
- [ ] Widget file copied to DDP-API static directory
- [ ] DDP-API configured to serve `/widget` static files
- [ ] VoteBot WebSocket endpoint accessible at `/ws/chat`
- [ ] CORS configured to allow widget connections from embedding sites
- [ ] SSL/TLS configured for `wss://` connections
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
├── widget.js      # Entry point, Shadow DOM, initialization
├── websocket.js   # WebSocket connection with auto-reconnect
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
| `src/widget.js` | Entry point, reads config, creates Shadow DOM |
| `src/websocket.js` | WebSocket with reconnection logic |
| `src/chat.js` | Message handling, streaming state |
| `src/ui.js` | DOM manipulation, markdown parser |
| `src/styles.css` | Widget styles |
| `build.js` | Build script (concat + minify) |
| `test.html` | Local testing page |

### Build Process

The `build.js` script:
1. Reads CSS and inlines it as a JavaScript string
2. Concatenates all JS modules in dependency order
3. Minifies with terser
4. Outputs single file to `dist/ddp-chat.min.js`

### Testing Changes

1. Make changes to source files in `src/`
2. Run `npm run build`
3. Refresh `test.html` in browser
4. Test with VoteBot running locally
