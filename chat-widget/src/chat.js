/**
 * Chat message handling and streaming logic.
 */

const DDPChat = (function() {
    let isStreaming = false;
    let pageContext = { type: 'general' };
    let handoffActive = false;
    let onUIUpdateCallback = null;

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
     * @returns {boolean} - Whether the message was sent
     */
    function sendMessage(message) {
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

        // Send to server
        const sent = DDPWebSocket.send({
            type: 'user_message',
            payload: {
                message: message,
                page_context: pageContext
            }
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
                // Restore previous messages
                if (data.payload.messages && onUIUpdateCallback) {
                    data.payload.messages.forEach(function(msg) {
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
        getHandoffActive: getHandoffActive
    };
})();
