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

    // SessionStorage persistence — scoped to browser tab, cleared on tab close
    const STORAGE_PREFIX = 'ddp_votebot_';

    // localStorage for cross-session visitor identity (distinct from sessionStorage session_id)
    const VISITOR_KEY = 'ddp_votebot_visitor_id';

    function _getOrCreateVisitorId() {
        try {
            var vid = localStorage.getItem(VISITOR_KEY);
            if (!vid) {
                // crypto.randomUUID may not be available in all browsers
                if (typeof crypto !== 'undefined' && crypto.randomUUID) {
                    vid = 'v_' + crypto.randomUUID().replace(/-/g, '').slice(0, 12);
                } else {
                    // Fallback: generate a random hex string
                    vid = 'v_' + Math.random().toString(16).slice(2, 14);
                }
                localStorage.setItem(VISITOR_KEY, vid);
            }
            return vid;
        } catch (e) {
            return null; // Private browsing or storage disabled
        }
    }
    const SESSION_TIMEOUT_MS = 30 * 60 * 1000; // 30 minutes

    function _storageGet(key) {
        try { return sessionStorage.getItem(STORAGE_PREFIX + key); } catch (e) { return null; }
    }
    function _storageSet(key, value) {
        try { sessionStorage.setItem(STORAGE_PREFIX + key, value); } catch (e) {}
    }
    function _storageRemove(key) {
        try { sessionStorage.removeItem(STORAGE_PREFIX + key); } catch (e) {}
    }
    function _isSessionExpired() {
        var lastActivity = _storageGet('last_activity');
        if (!lastActivity) return true;
        return (Date.now() - parseInt(lastActivity, 10)) > SESSION_TIMEOUT_MS;
    }
    function _touchActivity() {
        _storageSet('last_activity', String(Date.now()));
    }
    function _restoreSession() {
        if (_isSessionExpired()) {
            _storageRemove('session_id');
            _storageRemove('last_activity');
            _storageRemove('page_context');
            _storageRemove('popup_open');
            return null;
        }
        return _storageGet('session_id');
    }

    /**
     * Initialize WebSocket connection.
     * @param {string} wsUrl - WebSocket server URL
     * @param {Function} onMessage - Callback for incoming messages
     * @param {Function} onStatusChange - Callback for connection status changes
     */
    function connect(wsUrl, onMessage, onStatusChange) {
        onMessageCallback = onMessage;
        onStatusChangeCallback = onStatusChange;

        // Restore session ID from storage if available
        if (!sessionId) {
            sessionId = _restoreSession();
        }

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
                    _storageSet('session_id', sessionId);
                    _touchActivity();
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
            _touchActivity();
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
        getVisitorId: _getOrCreateVisitorId,
        disconnect: disconnect,
        storageGet: _storageGet,
        storageSet: _storageSet,
        storageRemove: _storageRemove,
        touchActivity: _touchActivity,
        isSessionExpired: _isSessionExpired
    };
})();
