/**
 * DDP Chat Widget - Main Entry Point
 *
 * Embeddable chat widget for Digital Democracy Project VoteBot.
 * Uses Shadow DOM for style isolation.
 *
 * Two modes of operation:
 * 1. Website Mode (autoDetect: true) - Automatically detects page context from URL, meta tags, or DOM
 * 2. Mobile App Mode - Explicitly pass pageContext with bill/legislator details
 *
 * Usage (Website with auto-detection):
 * <script>
 *   window.DDPChatConfig = {
 *     wsUrl: 'wss://api.digitaldemocracyproject.org/votebot/ws',
 *     autoDetect: true
 *   };
 * </script>
 *
 * Usage (Mobile App with explicit context):
 * <script>
 *   window.DDPChatConfig = {
 *     wsUrl: 'wss://api.digitaldemocracyproject.org/votebot/ws',
 *     pageContext: {
 *       type: 'bill',
 *       id: 'HR 1',
 *       title: 'One Big Beautiful Bill Act',
 *       jurisdiction: 'US'
 *     }
 *   };
 * </script>
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
        welcomeMessage: null,  // null = auto-generate based on context
        pageContext: null,     // null = use autoDetect or default to general
        autoDetect: false      // true = detect context from page
    };

    // Merge user config with defaults
    var config = Object.assign({}, defaultConfig, window.DDPChatConfig || {});

    // CSS will be injected by build script
    var WIDGET_CSS = '/* CSS_PLACEHOLDER */';

    /**
     * Auto-detect page context from the current page.
     * Checks URL patterns, meta tags, data attributes, and JSON-LD.
     * @returns {Object} Detected page context
     */
    function autoDetectPageContext() {
        var context = { type: 'general' };
        var url = window.location.href;
        var pathname = window.location.pathname;

        // 1. Check for DDP data attributes on body or container
        var ddpElement = document.querySelector('[data-ddp-type]');
        if (ddpElement) {
            context.type = ddpElement.getAttribute('data-ddp-type') || 'general';
            context.id = ddpElement.getAttribute('data-ddp-id');
            context.title = ddpElement.getAttribute('data-ddp-title');
            context.jurisdiction = ddpElement.getAttribute('data-ddp-jurisdiction');
            context.url = url;
            console.log('[DDPChat] Context detected from data attributes:', context);
            return context;
        }

        // 2. Check for JSON-LD structured data
        var jsonLdScripts = document.querySelectorAll('script[type="application/ld+json"]');
        for (var i = 0; i < jsonLdScripts.length; i++) {
            try {
                var data = JSON.parse(jsonLdScripts[i].textContent);
                if (data['@type'] === 'Legislation' || data['@type'] === 'Bill') {
                    context.type = 'bill';
                    context.title = data.name || data.headline;
                    context.id = data.identifier || data.legislationIdentifier;
                    context.url = url;
                    console.log('[DDPChat] Context detected from JSON-LD:', context);
                    return context;
                }
                if (data['@type'] === 'Person' && data.jobTitle) {
                    context.type = 'legislator';
                    context.title = data.name;
                    context.id = data.identifier;
                    context.url = url;
                    console.log('[DDPChat] Context detected from JSON-LD:', context);
                    return context;
                }
            } catch (e) {
                // Invalid JSON, skip
            }
        }

        // 3. Check meta tags
        var ogType = document.querySelector('meta[property="og:type"]');
        var ogTitle = document.querySelector('meta[property="og:title"]');
        if (ogType && ogTitle) {
            var typeValue = ogType.getAttribute('content');
            if (typeValue === 'article' || typeValue === 'legislation') {
                // Check if it looks like a bill page
                if (pathname.match(/\/bill[s]?\//i) || pathname.match(/\/legislation\//i)) {
                    context.type = 'bill';
                    context.title = ogTitle.getAttribute('content');
                    context.url = url;
                }
            }
        }

        // 4. Check URL patterns for common DDP routes
        // Bill patterns: /bill/HR-1, /bills/FL/HB-1234, /legislation/US-HR-1
        var billMatch = pathname.match(/\/bill[s]?\/(?:([A-Z]{2})[-\/])?([A-Z]+[-\s]?\d+)/i);
        if (billMatch) {
            context.type = 'bill';
            context.jurisdiction = billMatch[1] || null;
            context.id = billMatch[2];
            context.title = extractPageTitle('bill');
            context.url = url;
            console.log('[DDPChat] Context detected from URL (bill):', context);
            return context;
        }

        // Legislator patterns: /legislator/john-smith, /legislators/FL/john-smith
        var legMatch = pathname.match(/\/legislator[s]?\/(?:([A-Z]{2})[-\/])?([a-z0-9-]+)/i);
        if (legMatch) {
            context.type = 'legislator';
            context.jurisdiction = legMatch[1] || null;
            context.id = legMatch[2];
            context.title = extractPageTitle('legislator');
            context.url = url;
            console.log('[DDPChat] Context detected from URL (legislator):', context);
            return context;
        }

        // Organization patterns: /organization/nra, /org/aclu
        var orgMatch = pathname.match(/\/org(?:anization)?[s]?\/([a-z0-9-]+)/i);
        if (orgMatch) {
            context.type = 'organization';
            context.id = orgMatch[1];
            context.title = extractPageTitle('organization');
            context.url = url;
            console.log('[DDPChat] Context detected from URL (organization):', context);
            return context;
        }

        console.log('[DDPChat] No specific context detected, using general');
        return context;
    }

    /**
     * Extract page title from DOM, cleaning up common prefixes/suffixes.
     * @param {string} type - The context type for smarter extraction
     * @returns {string|null} Extracted title
     */
    function extractPageTitle(type) {
        // Try og:title first (usually cleaner)
        var ogTitle = document.querySelector('meta[property="og:title"]');
        if (ogTitle) {
            return cleanTitle(ogTitle.getAttribute('content'));
        }

        // Try the main h1
        var h1 = document.querySelector('h1');
        if (h1) {
            return cleanTitle(h1.textContent);
        }

        // Fall back to document title
        return cleanTitle(document.title);
    }

    /**
     * Clean up a title by removing common site suffixes.
     * @param {string} title - Raw title
     * @returns {string} Cleaned title
     */
    function cleanTitle(title) {
        if (!title) return null;
        // Remove common suffixes like " | Site Name" or " - Site Name"
        return title
            .replace(/\s*[|\-–—]\s*(Digital Democracy|DDP|OpenStates|Congress\.gov).*$/i, '')
            .replace(/\s*[|\-–—]\s*[^|–—-]+$/, '')
            .trim();
    }

    /**
     * Generate a personalized welcome message based on page context.
     * @param {Object} context - Page context
     * @returns {string} Welcome message
     */
    function generateWelcomeMessage(context) {
        if (!context || context.type === 'general') {
            return 'Welcome! Ask me anything about legislation, legislators, or civic engagement.';
        }

        var title = context.title;
        var id = context.id;

        switch (context.type) {
            case 'bill':
                if (title && id) {
                    return 'Welcome! I can answer detailed questions about **' + title + ' (' + id + ')**. You can also ask me about other bills, legislators, or Digital Democracy Project in general.';
                } else if (title) {
                    return 'Welcome! I can answer detailed questions about **' + title + '**. You can also ask me about other bills, legislators, or Digital Democracy Project in general.';
                } else if (id) {
                    return 'Welcome! I can answer detailed questions about **' + id + '**. You can also ask me about other bills, legislators, or Digital Democracy Project in general.';
                }
                return 'Welcome! I can answer detailed questions about this bill. You can also ask me about other legislation, legislators, or Digital Democracy Project in general.';

            case 'legislator':
                if (title) {
                    return 'Welcome! I can answer questions about **' + title + '**, including their voting record, sponsored bills, and positions. You can also ask me about other legislators or legislation.';
                }
                return 'Welcome! I can answer questions about this legislator, including their voting record, sponsored bills, and positions. You can also ask me about other legislators or legislation.';

            case 'organization':
                if (title) {
                    return 'Welcome! I can provide information about **' + title + '**, including their legislative positions and supported bills. You can also ask me about legislators or legislation.';
                }
                return 'Welcome! I can provide information about this organization, including their legislative positions and supported bills. You can also ask me about legislators or legislation.';

            default:
                return 'Welcome! Ask me anything about legislation, legislators, or civic engagement.';
        }
    }

    /**
     * Parse URL parameters for page context.
     * Supports: ?ddp_type=bill&ddp_id=HR%201&ddp_title=My%20Bill&ddp_jurisdiction=US
     * Or: ?ddp_url=https://digitaldemocracyproject.org/bills/my-bill
     * @returns {Object|null} Context from URL params, or null if not present
     */
    function getContextFromUrlParams() {
        var params = new URLSearchParams(window.location.search);

        // Check for ddp_url first (will be resolved async)
        var ddpUrl = params.get('ddp_url');
        if (ddpUrl) {
            return { _ddp_url: ddpUrl };  // Special marker for async resolution
        }

        var type = params.get('ddp_type');
        if (!type) return null;

        var context = {
            type: type,
            id: params.get('ddp_id'),
            title: params.get('ddp_title'),
            jurisdiction: params.get('ddp_jurisdiction'),
            url: window.location.href
        };

        // Clean up null values
        Object.keys(context).forEach(function(key) {
            if (context[key] === null) delete context[key];
        });

        console.log('[DDPChat] Context from URL params:', context);
        return context;
    }

    /**
     * Resolve a DDP URL to page context via the API.
     * @param {string} ddpUrl - DDP URL to resolve
     * @returns {Promise<Object>} Resolved context
     */
    function resolveContextFromUrl(ddpUrl) {
        // Build the API URL - use same origin as wsUrl or fall back to production
        var apiBase = config.wsUrl
            .replace('wss://', 'https://')
            .replace('ws://', 'http://')
            .replace(/\/ws.*$/, '');

        var resolveUrl = apiBase + '/votebot/v1/content/resolve?url=' + encodeURIComponent(ddpUrl);

        console.log('[DDPChat] Resolving DDP URL:', ddpUrl);

        return fetch(resolveUrl)
            .then(function(response) {
                if (!response.ok) {
                    throw new Error('Failed to resolve URL: ' + response.status);
                }
                return response.json();
            })
            .then(function(data) {
                console.log('[DDPChat] Resolved context:', data);
                return data;
            })
            .catch(function(error) {
                console.error('[DDPChat] Failed to resolve DDP URL:', error);
                return { type: 'general' };
            });
    }

    /**
     * Resolve the final page context from URL params, config, or auto-detection.
     * Priority: URL params > explicit config > autoDetect > general
     * @returns {Object|Promise<Object>} Final page context (may be a promise if ddp_url needs resolution)
     */
    function resolvePageContext() {
        // 1. Check URL parameters first (highest priority)
        var urlContext = getContextFromUrlParams();
        if (urlContext) {
            // Check if this needs async resolution
            if (urlContext._ddp_url) {
                return resolveContextFromUrl(urlContext._ddp_url);
            }
            return urlContext;
        }

        // 2. If explicit pageContext provided, use it (mobile app mode)
        if (config.pageContext && config.pageContext.type) {
            console.log('[DDPChat] Using explicit pageContext:', config.pageContext);
            return config.pageContext;
        }

        // 3. If autoDetect enabled, detect from page
        if (config.autoDetect) {
            return autoDetectPageContext();
        }

        // 4. Default to general
        return { type: 'general' };
    }

    /**
     * Initialize the widget (async to support URL resolution).
     */
    function initWidget() {
        // Resolve page context (may be async)
        var contextResult = resolvePageContext();

        // Handle both sync and async context resolution
        if (contextResult && typeof contextResult.then === 'function') {
            // Async - wait for resolution
            contextResult.then(function(pageContext) {
                initWidgetWithContext(pageContext);
            });
        } else {
            // Sync - proceed immediately
            initWidgetWithContext(contextResult);
        }
    }

    /**
     * Initialize the widget with resolved context.
     * @param {Object} pageContext - Resolved page context
     */
    function initWidgetWithContext(pageContext) {
        // Resolve welcome message
        var welcomeMessage = config.welcomeMessage;
        if (!welcomeMessage) {
            welcomeMessage = generateWelcomeMessage(pageContext);
        }

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

        // Initialize chat module with resolved context
        DDPChat.init(DDPUI.handleUIUpdate, pageContext);

        // Connect WebSocket
        DDPWebSocket.connect(
            config.wsUrl,
            DDPChat.handleServerMessage,
            DDPUI.updateStatus
        );

        // Set up event listeners
        setupEventListeners();

        // Show welcome message
        if (welcomeMessage) {
            DDPUI.addSystemMessage(welcomeMessage);
        }

        console.log('[DDPChat] Widget initialized with context:', pageContext);
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
         * Update page context and optionally refresh welcome message.
         * @param {Object} context - New page context
         * @param {boolean} showWelcome - Whether to show a new welcome message (default: false)
         */
        setPageContext: function(context, showWelcome) {
            DDPChat.setPageContext(context);
            if (showWelcome) {
                var message = generateWelcomeMessage(context);
                DDPUI.addSystemMessage(message);
            }
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
        },

        /**
         * Generate a welcome message for a given context.
         * Useful for mobile apps that want to customize the message.
         * @param {Object} context - Page context
         * @returns {string}
         */
        generateWelcomeMessage: function(context) {
            return generateWelcomeMessage(context);
        }
    };

    // Initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initWidget);
    } else {
        initWidget();
    }
})();
