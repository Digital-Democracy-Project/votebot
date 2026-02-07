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
        avatar: 'data:image/png;base64,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',
        welcomeMessage: null,  // null = auto-generate based on context
        pageContext: null,     // null = use autoDetect or default to general
        autoDetect: false,     // true = detect context from page
        autoOpen: false        // true = open chat popup automatically on load
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
     * Check if a title already contains the identifier (bill number).
     * Handles variations like "HR1" vs "HR 1" by normalizing whitespace.
     * @param {string} title - The title to check
     * @param {string} id - The identifier to look for
     * @returns {boolean} True if title contains the id
     */
    function titleContainsId(title, id) {
        if (!title || !id) return false;
        var normalizedTitle = title.toLowerCase().replace(/\s+/g, '');
        var normalizedId = id.toLowerCase().replace(/\s+/g, '');
        return normalizedTitle.indexOf(normalizedId) !== -1;
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
                if (title && id && !titleContainsId(title, id)) {
                    // Only append ID if title doesn't already contain it
                    return 'Welcome! I can answer detailed questions about **' + title + ' (' + id + ')**.';
                } else if (title) {
                    return 'Welcome! I can answer detailed questions about **' + title + '**.';
                } else if (id) {
                    return 'Welcome! I can answer detailed questions about **' + id + '**.';
                }
                return 'Welcome! I can answer detailed questions about this bill.';

            case 'legislator':
                if (title) {
                    return 'Welcome! I can answer questions about **' + title + '**.';
                }
                return 'Welcome! I can answer questions about this legislator.';

            case 'organization':
                if (title) {
                    return 'Welcome! I can provide information about **' + title + '**.';
                }
                return 'Welcome! I can provide information about this organization.';

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
     * Compare two page contexts to detect navigation to a different entity.
     * @param {Object} oldCtx - Previous page context
     * @param {Object} newCtx - New page context
     * @returns {boolean} True if the context has changed
     */
    function contextChanged(oldCtx, newCtx) {
        if (!oldCtx || !newCtx) return true;
        if (oldCtx.type !== newCtx.type) return true;
        if (newCtx.type !== 'general') {
            if (oldCtx.slug && newCtx.slug) return oldCtx.slug !== newCtx.slug;
            if (oldCtx.id && newCtx.id) return oldCtx.id !== newCtx.id;
            if (oldCtx.webflow_id && newCtx.webflow_id) return oldCtx.webflow_id !== newCtx.webflow_id;
            if (oldCtx.title && newCtx.title) return oldCtx.title !== newCtx.title;
        }
        return false;
    }

    /**
     * Generate a context-change notification message (not a welcome message).
     * @param {Object} context - New page context
     * @returns {string} Context-change notification
     */
    function generateContextChangeMessage(context) {
        if (!context || context.type === 'general') {
            return "You're now on a general page. I can answer questions about any legislation, legislator, or organization.";
        }

        var title = context.title;
        var id = context.id;

        switch (context.type) {
            case 'bill':
                if (title && id && !titleContainsId(title, id)) {
                    return "You're now viewing **" + title + ' (' + id + ")**. I can answer questions about this bill.";
                } else if (title) {
                    return "You're now viewing **" + title + "**. I can answer questions about this bill.";
                } else if (id) {
                    return "You're now viewing **" + id + "**. I can answer questions about this bill.";
                }
                return "You're now viewing a bill. I can answer questions about it.";

            case 'legislator':
                if (title) {
                    return "You're now viewing **" + title + "**. I can answer questions about this legislator.";
                }
                return "You're now viewing a legislator. I can answer questions about them.";

            case 'organization':
                if (title) {
                    return "You're now viewing **" + title + "**. I can answer questions about this organization.";
                }
                return "You're now viewing an organization. I can answer questions about it.";

            default:
                return "You're now on a general page. I can answer questions about any legislation, legislator, or organization.";
        }
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
        // Detect returning session
        var isReturning = !DDPWebSocket.isSessionExpired() && !!DDPWebSocket.storageGet('session_id');

        // Read previous context from storage
        var previousContext = null;
        var previousContextJson = DDPWebSocket.storageGet('page_context');
        if (previousContextJson) {
            try { previousContext = JSON.parse(previousContextJson); } catch (e) {}
        }

        // Persist NEW context to storage
        DDPWebSocket.storageSet('page_context', JSON.stringify(pageContext));

        // Decide initial message
        var initialMessage = null;
        var contextChangeMsg = null;
        if (!isReturning) {
            // New session — show welcome message
            initialMessage = config.welcomeMessage || generateWelcomeMessage(pageContext);
        } else if (contextChanged(previousContext, pageContext)) {
            // Returning session + different page — show context-change notice after restore
            contextChangeMsg = generateContextChangeMessage(pageContext);
        }
        // Returning + same context → no extra message

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

        // Connect WebSocket with intercepting message handler for returning sessions
        var originalHandler = DDPChat.handleServerMessage;
        var wrappedHandler = isReturning ? function(data) {
            // Intercept session_info to detect server-side session loss
            if (data.type === 'session_info' && !data.payload.restored) {
                // Server lost the session — show welcome message instead
                originalHandler(data);
                DDPUI.addSystemMessage(config.welcomeMessage || generateWelcomeMessage(pageContext));
                // Unwrap — no longer need interception
                // Further messages go directly to original handler
                return;
            }

            // After session_restored, append context-change message
            if (data.type === 'session_restored') {
                originalHandler(data);
                if (contextChangeMsg) {
                    DDPUI.addSystemMessage(contextChangeMsg);
                }
                return;
            }

            originalHandler(data);
        } : originalHandler;

        DDPWebSocket.connect(
            config.wsUrl,
            wrappedHandler,
            DDPUI.updateStatus
        );

        // Set up event listeners
        setupEventListeners();

        // Show welcome/initial message for new sessions
        if (initialMessage) {
            DDPUI.addSystemMessage(initialMessage);
        }

        // Restore popup open state for returning sessions
        if (isReturning && DDPWebSocket.storageGet('popup_open') === '1') {
            DDPUI.openPopup();
        } else if (config.autoOpen) {
            DDPUI.openPopup();
        }

        console.log('[DDPChat] Widget initialized with context:', pageContext, isReturning ? '(returning session)' : '(new session)');
    }

    /**
     * Set up UI event listeners.
     */
    function setupEventListeners() {
        var elements = DDPUI.getElements();

        // Chat button click
        elements.chatButton.addEventListener('click', function() {
            DDPUI.togglePopup();
            var isOpen = elements.chatPopup.classList.contains('open');
            DDPWebSocket.storageSet('popup_open', isOpen ? '1' : '0');
        });

        // Close button click
        elements.closeButton.addEventListener('click', function() {
            DDPUI.closePopup();
            DDPWebSocket.storageSet('popup_open', '0');
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
            DDPWebSocket.storageSet('page_context', JSON.stringify(context));
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
