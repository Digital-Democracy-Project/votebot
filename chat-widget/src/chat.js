/**
 * Chat message handling and streaming logic.
 */

const DDPChat = (function() {
    let isStreaming = false;
    let pageContext = { type: 'general' };
    let handoffActive = false;
    let onUIUpdateCallback = null;
    let isFirstMessage = true;
    let pageLoadTime = Date.now();

    function _extractDomain(url) {
        if (!url) return null;
        try {
            return new URL(url).hostname;
        } catch (e) {
            return null;
        }
    }

    /**
     * An address without its query string, fragment and credentials: all the server may keep (VOTEBOT-63).
     * A page address can carry a sign-in token or personal data, and an address is logged and posted to Slack.
     * @param {string} value - An address
     * @returns {string|null} "origin/path", or null when it is not an http(s) address
     */
    function pageAddress(value) {
        try {
            var address = new URL(value);
            if (address.protocol !== 'https:' && address.protocol !== 'http:') return null;
            return address.origin + address.pathname;
        } catch (e) {
            return null;
        }
    }

    /**
     * The page context as it is sent: its url (set by auto-detection, a ?ddp_url= link, the host page
     * or /content/resolve) reduced to origin and path.
     * @param {Object} context - A page context
     * @returns {Object} The context, or a copy with the url reduced (or left out when it is not an address)
     */
    function contextForServer(context) {
        if (!context || typeof context.url !== 'string') return context;
        var copy = Object.assign({}, context);
        var address = pageAddress(context.url);
        if (address) {
            copy.url = address;
        } else {
            delete copy.url;
        }
        return copy;
    }

    function _getScrollDepth() {
        try {
            var scrollTop = window.pageYOffset || document.documentElement.scrollTop;
            var docHeight = Math.max(
                document.body.scrollHeight, document.documentElement.scrollHeight,
                document.body.offsetHeight, document.documentElement.offsetHeight
            );
            var winHeight = window.innerHeight;
            if (docHeight <= winHeight) return 1.0;
            return Math.min(1.0, Math.round((scrollTop / (docHeight - winHeight)) * 100) / 100);
        } catch (e) {
            return null;
        }
    }

    function _getTimeOnPage() {
        return Math.round((Date.now() - pageLoadTime) / 1000);
    }

    /**
     * Initialize chat module.
     * @param {Function} onUIUpdate - Callback for UI updates
     * @param {Object} initialContext - Initial page context
     */
    function init(onUIUpdate, initialContext) {
        onUIUpdateCallback = onUIUpdate;
        if (initialContext) {
            pageContext = initialContext;
        }
    }

    /**
     * Send a user message.
     * @param {string} message - The message text
     * @param {string|null} button - Optional quick-action button id
     *     ('summary', 'pros_cons', 'status_votes'). null for free-typed messages.
     * @returns {boolean} - Whether the message was sent
     */
    function sendMessage(message, button) {
        message = message.trim();
        if (!message || !DDPWebSocket.isConnected() || isStreaming) {
            return false;
        }

        // Show user message in UI
        if (onUIUpdateCallback) {
            onUIUpdateCallback({
                type: 'user_message',
                payload: { message: message }
            });
        }

        // Build payload with analytics fields
        var msgPayload = {
            message: message,
            page_context: contextForServer(pageContext),
            visitor_id: DDPWebSocket.getVisitorId(),
            page_url: pageAddress(window.location.href),
            scroll_depth: _getScrollDepth(),
            time_on_page: _getTimeOnPage()
        };
        if (button) {
            msgPayload.button = button;
        }
        // entry_referrer only on first message per session
        if (isFirstMessage) {
            var ref = _extractDomain(document.referrer);
            if (ref) {
                msgPayload.entry_referrer = ref;
            }
            isFirstMessage = false;
        }

        // Send to server
        const sent = DDPWebSocket.send({
            type: 'user_message',
            payload: msgPayload
        });

        if (sent) {
            // Only disable input and set streaming if NOT in handoff mode
            // During handoff, messages are relayed to Slack without bot processing
            if (!handoffActive) {
                isStreaming = true;
                if (onUIUpdateCallback) {
                    onUIUpdateCallback({ type: 'input_disable' });
                }
            }
        }

        return sent;
    }

    /**
     * Handle incoming server message.
     * @param {Object} data - Server message
     */
    function handleServerMessage(data) {
        console.log('[DDPChat] Received:', data.type);

        switch (data.type) {
            case 'session_info':
                // Session established
                if (onUIUpdateCallback) {
                    onUIUpdateCallback({ type: 'input_enable' });
                }
                break;

            case 'session_restored':
                // Restore previous messages — skip system-role messages (LLM context injections,
                // not conversation history; rendering them would show internal nav notes to users)
                if (data.payload.messages && onUIUpdateCallback) {
                    data.payload.messages.forEach(function(msg) {
                        if (msg.role === 'system') return;
                        onUIUpdateCallback({
                            type: 'restored_message',
                            payload: {
                                role: msg.role,
                                content: msg.content
                            }
                        });
                    });
                }
                break;

            case 'stream_start':
                isStreaming = true;
                if (onUIUpdateCallback) {
                    onUIUpdateCallback({ type: 'typing_show' });
                }
                break;

            case 'stream_chunk':
                if (data.payload.text && onUIUpdateCallback) {
                    onUIUpdateCallback({
                        type: 'stream_chunk',
                        payload: { text: data.payload.text }
                    });
                }
                break;

            case 'stream_end':
                isStreaming = false;
                if (onUIUpdateCallback) {
                    onUIUpdateCallback({
                        type: 'stream_end',
                        payload: data.payload
                    });
                    onUIUpdateCallback({ type: 'input_enable' });
                }

                // Check for human handoff - show confirmation first
                if (data.payload.requires_human) {
                    if (onUIUpdateCallback) {
                        onUIUpdateCallback({
                            type: 'handoff_suggested',
                            payload: {}
                        });
                    }
                }
                break;

            case 'agent_joined':
                handoffActive = true;
                if (onUIUpdateCallback) {
                    onUIUpdateCallback({
                        type: 'agent_joined',
                        payload: data.payload
                    });
                }
                break;

            case 'agent_message':
                if (onUIUpdateCallback) {
                    onUIUpdateCallback({
                        type: 'agent_message',
                        payload: data.payload
                    });
                }
                break;

            case 'agent_left':
                handoffActive = false;
                isStreaming = false;  // Reset streaming state
                if (onUIUpdateCallback) {
                    onUIUpdateCallback({
                        type: 'agent_left',
                        payload: {}
                    });
                }
                break;

            case 'error':
                isStreaming = false;
                if (onUIUpdateCallback) {
                    onUIUpdateCallback({
                        type: 'error',
                        payload: data.payload
                    });
                    onUIUpdateCallback({ type: 'input_enable' });
                }
                break;

            case 'connection_failed':
                if (onUIUpdateCallback) {
                    onUIUpdateCallback({
                        type: 'system_message',
                        payload: { message: data.payload.message }
                    });
                }
                break;

            case 'pong':
                // Heartbeat response, no action needed
                break;
        }
    }

    /**
     * Update page context.
     * @param {Object} context - New page context
     */
    function setPageContext(context) {
        pageContext = context;
        pageLoadTime = Date.now(); // Reset time-on-page when context changes
    }

    /**
     * Get current page context.
     * @returns {Object}
     */
    function getPageContext() {
        return pageContext;
    }

    /**
     * Check if currently streaming.
     * @returns {boolean}
     */
    function getIsStreaming() {
        return isStreaming;
    }

    /**
     * Check if handoff is active.
     * @returns {boolean}
     */
    function getHandoffActive() {
        return handoffActive;
    }

    return {
        init: init,
        sendMessage: sendMessage,
        handleServerMessage: handleServerMessage,
        setPageContext: setPageContext,
        getPageContext: getPageContext,
        getIsStreaming: getIsStreaming,
        getHandoffActive: getHandoffActive,
        contextForServer: contextForServer
    };
})();
