# VoteBot Chat Widget - Proof of Concept

A standalone chat widget demonstrating streaming responses from VoteBot via WebSocket.

## Features

- Real-time streaming responses (text appears as it's generated)
- Conversation history
- Citation display with source attribution
- Confidence scoring visualization
- Human handoff detection
- Page context awareness (general, bill, legislator)
- Auto-reconnection on disconnect
- Responsive design

## Prerequisites

- VoteBot backend running on `localhost:8000`
- Modern web browser with WebSocket support

## Quick Start

1. **Start the VoteBot backend:**

   ```bash
   cd /path/to/votebot
   PYTHONPATH=src uvicorn votebot.main:app --host 0.0.0.0 --port 8000 --reload
   ```

2. **Open the widget:**

   Simply open `index.html` in your browser:
   - Double-click the file, or
   - Use a local server: `python -m http.server 3000` then visit `http://localhost:3000`

3. **Test the chat:**
   - Type a message and press Enter or click Send
   - Watch the streaming response appear in real-time
   - Try different page contexts using the buttons at the top

## Testing Scenarios

### General Questions
- "What is the Digital Democracy Project?"
- "How does DDP score legislators?"
- "What states does DDP operate in?"

### Bill Context (click "Bill: HB-1234" first)
- "What does this bill do?"
- "Who sponsored this bill?"
- "What are the arguments for and against?"

### Human Handoff Trigger
- "I want to talk to a human"
- "Can I speak to someone?"
- "This is useless" (frustration trigger)

## WebSocket Protocol

### Client → Server

```json
{
  "type": "user_message",
  "payload": {
    "message": "What is DDP?",
    "page_context": {
      "type": "general",
      "id": null,
      "title": null
    }
  }
}
```

### Server → Client

```json
// Stream start
{"type": "stream_start"}

// Stream chunk (multiple)
{"type": "stream_chunk", "payload": {"text": "The Digital "}}

// Stream end
{
  "type": "stream_end",
  "payload": {
    "citations": [...],
    "confidence": 0.85,
    "requires_human": false
  }
}
```

## Configuration

Edit the `CONFIG` object in `index.html` to customize:

```javascript
const CONFIG = {
    wsUrl: 'ws://localhost:8000/ws/chat',  // WebSocket endpoint
    reconnectDelay: [1000, 2000, 4000, 8000],  // Reconnection delays (ms)
    maxReconnectAttempts: 4,  // Max reconnection attempts
};
```

## File Structure

```
chat-widget-poc/
├── index.html     # Complete widget (HTML + CSS + JS)
└── README.md      # This file
```

## Notes

- This is a proof-of-concept; production use would require:
  - Build system (React/Vue/etc.)
  - Authentication
  - Error handling improvements
  - Accessibility features
  - Mobile optimization
  - Analytics integration
