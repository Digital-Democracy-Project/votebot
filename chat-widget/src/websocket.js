/**
 * WebSocket connection management with automatic reconnection.
 */

const DDPWebSocket = (function() {
    let ws = null;
    let sessionId = null;
    let reconnectAttempts = 0;
    let heartbeatInterval = null;
    let onMessageCallback = null;
    let onStatusChangeCallback = null;

    const reconnectDelays = [1000, 2000, 4000, 8000];
    const maxReconnectAttempts = 4;
    const heartbeatIntervalMs = 30000;

    /**
     * Initialize WebSocket connection.
     * @param {string} wsUrl - WebSocket server URL
     * @param {Function} onMessage - Callback for incoming messages
     * @param {Function} onStatusChange - Callback for connection status changes
     */
    function connect(wsUrl, onMessage, onStatusChange) {
        onMessageCallback = onMessage;
        onStatusChangeCallback = onStatusChange;

        _connect(wsUrl);
    }

    function _connect(wsUrl) {
        if (onStatusChangeCallback) {
            onStatusChangeCallback('connecting');
        }

        const url = sessionId
            ? `${wsUrl}?session_id=${sessionId}`
            : wsUrl;

        ws = new WebSocket(url);

        ws.onopen = function() {
            console.log('[DDPChat] WebSocket connected');
            reconnectAttempts = 0;

            if (onStatusChangeCallback) {
                onStatusChangeCallback('connected');
            }

            // Start heartbeat
            _startHeartbeat();
        };

        ws.onclose = function(event) {
            console.log('[DDPChat] WebSocket closed', event.code, event.reason);
            _stopHeartbeat();

            if (onStatusChangeCallback) {
                onStatusChangeCallback('disconnected');
            }

            // Attempt reconnection
            if (reconnectAttempts < maxReconnectAttempts) {
                const delay = reconnectDelays[reconnectAttempts] || reconnectDelays[reconnectDelays.length - 1];
                console.log(`[DDPChat] Reconnecting in ${delay}ms...`);

                setTimeout(function() {
                    reconnectAttempts++;
                    _connect(wsUrl);
                }, delay);
            } else {
                console.error('[DDPChat] Max reconnection attempts reached');
                if (onMessageCallback) {
                    onMessageCallback({
                        type: 'connection_failed',
                        payload: { message: 'Connection lost. Please refresh the page.' }
                    });
                }
            }
        };

        ws.onerror = function(error) {
            console.error('[DDPChat] WebSocket error:', error);
        };

        ws.onmessage = function(event) {
            try {
                const data = JSON.parse(event.data);

                // Handle session info internally
                if (data.type === 'session_info') {
                    sessionId = data.payload.session_id;
                    console.log('[DDPChat] Session ID:', sessionId);
                }

                if (onMessageCallback) {
                    onMessageCallback(data);
                }
            } catch (e) {
                console.error('[DDPChat] Failed to parse message:', e);
            }
        };
    }

    function _startHeartbeat() {
        _stopHeartbeat();
        heartbeatInterval = setInterval(function() {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'ping' }));
            }
        }, heartbeatIntervalMs);
    }

    function _stopHeartbeat() {
        if (heartbeatInterval) {
            clearInterval(heartbeatInterval);
            heartbeatInterval = null;
        }
    }

    /**
     * Send a message through the WebSocket.
     * @param {Object} data - Message data to send
     * @returns {boolean} - Whether the message was sent
     */
    function send(data) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify(data));
            return true;
        }
        return false;
    }

    /**
     * Check if WebSocket is connected.
     * @returns {boolean}
     */
    function isConnected() {
        return ws && ws.readyState === WebSocket.OPEN;
    }

    /**
     * Get current session ID.
     * @returns {string|null}
     */
    function getSessionId() {
        return sessionId;
    }

    /**
     * Disconnect WebSocket.
     */
    function disconnect() {
        _stopHeartbeat();
        if (ws) {
            ws.close();
            ws = null;
        }
    }

    return {
        connect: connect,
        send: send,
        isConnected: isConnected,
        getSessionId: getSessionId,
        disconnect: disconnect
    };
})();
