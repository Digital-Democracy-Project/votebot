/**
 * DDP Chat Widget - Main Entry Point
 *
 * Embeddable chat widget for Digital Democracy Project VoteBot.
 * Uses Shadow DOM for style isolation.
 *
 * Usage:
 * <script>
 *   window.DDPChatConfig = {
 *     wsUrl: 'wss://api.digitaldemocracyproject.org/votebot/ws',
 *     position: 'bottom-right',
 *     primaryColor: '#1a5f7a',
 *     pageContext: {
 *       type: 'bill',
 *       id: 'FL-HB-1234',
 *       title: 'Education Funding Act'
 *     }
 *   };
 * </script>
 * <script src="https://api.digitaldemocracyproject.org/widget/ddp-chat.min.js" async></script>
 */

(function() {
    'use strict';

    // Prevent multiple initializations
    if (window.DDPChatWidget) {
        console.warn('[DDPChat] Widget already initialized');
        return;
    }

    // Default configuration
    var defaultConfig = {
        wsUrl: 'wss://api.digitaldemocracyproject.org/votebot/ws',
        position: 'bottom-right',
        primaryColor: '#1a5f7a',
        botName: 'VoteBot',
        avatar: '\uD83D\uDDF3\uFE0F',
        welcomeMessage: 'Welcome! Ask me anything about legislation, legislators, or civic engagement.',
        pageContext: {
            type: 'general'
        }
    };

    // Merge user config with defaults
    var config = Object.assign({}, defaultConfig, window.DDPChatConfig || {});

    // CSS will be injected by build script
    var WIDGET_CSS = '/* CSS_PLACEHOLDER */';

    /**
     * Initialize the widget.
     */
    function initWidget() {
        // Create container element
        var container = document.createElement('div');
        container.id = 'ddp-chat-widget';

        // Attach shadow DOM for style isolation
        var shadowRoot = container.attachShadow({ mode: 'open' });

        // Inject styles
        var styleElement = document.createElement('style');
        styleElement.textContent = WIDGET_CSS;

        // Apply custom primary color if provided
        if (config.primaryColor && config.primaryColor !== defaultConfig.primaryColor) {
            styleElement.textContent = styleElement.textContent
                .replace(/--ddp-primary:\s*#[0-9a-fA-F]+/g, '--ddp-primary: ' + config.primaryColor);
        }

        shadowRoot.appendChild(styleElement);

        // Initialize UI module with shadow root
        DDPUI.init(shadowRoot);

        // Build and inject HTML
        var wrapper = document.createElement('div');
        wrapper.innerHTML = DDPUI.buildHTML(config);

        // Append all children to shadow root
        while (wrapper.firstChild) {
            shadowRoot.appendChild(wrapper.firstChild);
        }

        // Cache DOM elements
        DDPUI.cacheElements();

        // Add container to document
        document.body.appendChild(container);

        // Initialize chat module
        DDPChat.init(DDPUI.handleUIUpdate, config.pageContext);

        // Connect WebSocket
        DDPWebSocket.connect(
            config.wsUrl,
            DDPChat.handleServerMessage,
            DDPUI.updateStatus
        );

        // Set up event listeners
        setupEventListeners();

        // Show welcome message
        if (config.welcomeMessage) {
            DDPUI.addSystemMessage(config.welcomeMessage);
        }

        console.log('[DDPChat] Widget initialized');
    }

    /**
     * Set up UI event listeners.
     */
    function setupEventListeners() {
        var elements = DDPUI.getElements();

        // Chat button click
        elements.chatButton.addEventListener('click', function() {
            DDPUI.togglePopup();
        });

        // Close button click
        elements.closeButton.addEventListener('click', function() {
            DDPUI.closePopup();
        });

        // Send button click
        elements.sendButton.addEventListener('click', function() {
            var message = DDPUI.getInputValue();
            if (message.trim()) {
                DDPChat.sendMessage(message);
            }
        });

        // Enter to send (Shift+Enter for newline)
        elements.chatInput.addEventListener('keydown', function(e) {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                var message = DDPUI.getInputValue();
                if (message.trim()) {
                    DDPChat.sendMessage(message);
                }
            }
        });

        // Auto-resize textarea
        elements.chatInput.addEventListener('input', function() {
            DDPUI.autoResizeInput();
        });

        // Handle link clicks in messages (open in new tab)
        elements.messagesContainer.addEventListener('click', function(e) {
            if (e.target.tagName === 'A' && e.target.href) {
                e.preventDefault();
                window.open(e.target.href, '_blank', 'noopener,noreferrer');
            }
        });
    }

    // Public API
    window.DDPChatWidget = {
        /**
         * Open the chat popup.
         */
        open: function() {
            DDPUI.openPopup();
        },

        /**
         * Close the chat popup.
         */
        close: function() {
            DDPUI.closePopup();
        },

        /**
         * Toggle the chat popup.
         */
        toggle: function() {
            DDPUI.togglePopup();
        },

        /**
         * Update page context.
         * @param {Object} context - New page context
         */
        setPageContext: function(context) {
            DDPChat.setPageContext(context);
        },

        /**
         * Get current page context.
         * @returns {Object}
         */
        getPageContext: function() {
            return DDPChat.getPageContext();
        },

        /**
         * Check if widget is connected.
         * @returns {boolean}
         */
        isConnected: function() {
            return DDPWebSocket.isConnected();
        },

        /**
         * Get session ID.
         * @returns {string|null}
         */
        getSessionId: function() {
            return DDPWebSocket.getSessionId();
        }
    };

    // Initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initWidget);
    } else {
        initWidget();
    }
})();
