/**
 * UI module for DOM manipulation and rendering.
 */

const DDPUI = (function() {
    let shadowRoot = null;
    let elements = {};
    let currentStreamingMessage = null;
    let currentStreamingText = '';

    // Simple markdown parser (no external dependencies)
    const markdownParser = {
        parse: function(text) {
            if (!text) return '';

            let html = text;

            // Escape HTML entities first
            html = html
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;');

            // Code blocks (must be before other replacements)
            html = html.replace(/```(\w*)\n?([\s\S]*?)```/g, function(match, lang, code) {
                return '<pre><code>' + code.trim() + '</code></pre>';
            });

            // Inline code
            html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

            // Headers
            html = html.replace(/^#### (.+)$/gm, '<h4>$1</h4>');
            html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
            html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
            html = html.replace(/^# (.+)$/gm, '<h1>$1</h1>');

            // Bold and italic
            html = html.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
            html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
            html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
            html = html.replace(/___(.+?)___/g, '<strong><em>$1</em></strong>');
            html = html.replace(/__(.+?)__/g, '<strong>$1</strong>');
            html = html.replace(/_(.+?)_/g, '<em>$1</em>');

            // Markdown links [text](url)
            html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

            // Auto-link bare URLs (not already in an href)
            html = html.replace(/(^|[^"'>])(https?:\/\/[^\s<]+[^\s<.,;:!?"'\)\]])/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');

            // Blockquotes
            html = html.replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>');

            // Horizontal rule
            html = html.replace(/^---$/gm, '<hr>');

            // Unordered lists
            html = html.replace(/^[\*\-] (.+)$/gm, '<li>$1</li>');
            html = html.replace(/(<li>.*<\/li>)\n(?=<li>)/g, '$1');
            html = html.replace(/(<li>[\s\S]*?<\/li>)(?!\n<li>)/g, '<ul>$1</ul>');

            // Ordered lists
            html = html.replace(/^\d+\. (.+)$/gm, '<oli>$1</oli>');
            html = html.replace(/(<oli>.*<\/oli>)\n(?=<oli>)/g, '$1');
            html = html.replace(/(<oli>[\s\S]*?<\/oli>)(?!\n<oli>)/g, function(match) {
                return '<ol>' + match.replace(/<\/?oli>/g, function(tag) {
                    return tag === '<oli>' ? '<li>' : '</li>';
                }) + '</ol>';
            });

            // Paragraphs (lines not already wrapped)
            html = html.split('\n\n').map(function(para) {
                para = para.trim();
                if (!para) return '';
                if (para.match(/^<(h[1-4]|ul|ol|pre|blockquote|hr)/)) return para;
                if (!para.match(/^<[a-z]/)) {
                    return '<p>' + para.replace(/\n/g, '<br>') + '</p>';
                }
                return para;
            }).join('');

            // Clean up consecutive blockquotes
            html = html.replace(/<\/blockquote>\s*<blockquote>/g, '<br>');

            return html;
        }
    };

    /**
     * Initialize UI with shadow root.
     * @param {ShadowRoot} root - Shadow DOM root
     */
    function init(root) {
        shadowRoot = root;
    }

    /**
     * Build the chat widget HTML structure.
     * @param {Object} config - Widget configuration
     * @returns {string} - HTML string
     */
    function buildHTML(config) {
        const botName = config.botName || 'VoteBot';
        const avatar = config.avatar || '\uD83D\uDDF3\uFE0F';

        return `
            <button class="ddp-chat-button" aria-label="Open chat">
                ${avatar}
            </button>
            <div class="ddp-chat-popup">
                <div class="ddp-chat-header">
                    <div class="ddp-chat-header-avatar">${avatar}</div>
                    <div class="ddp-chat-header-info">
                        <h2>${botName}</h2>
                        <div class="ddp-status">
                            <span class="ddp-status-dot connecting"></span>
                            <span class="ddp-status-text">Connecting...</span>
                        </div>
                    </div>
                    <button class="ddp-close-button" aria-label="Close chat">&times;</button>
                </div>
                <div class="ddp-handoff-banner"></div>
                <div class="ddp-chat-messages"></div>
                <div class="ddp-chat-input-area">
                    <div class="ddp-chat-input-container">
                        <textarea
                            class="ddp-chat-input"
                            placeholder="Type your message..."
                            rows="1"
                            disabled
                        ></textarea>
                        <button class="ddp-send-button" disabled aria-label="Send message">
                            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
                                <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
                            </svg>
                        </button>
                    </div>
                </div>
                <div class="ddp-powered-by">
                    Powered by <a href="https://digitaldemocracyproject.org" target="_blank" rel="noopener">Digital Democracy Project</a>
                </div>
            </div>
        `;
    }

    /**
     * Cache DOM element references.
     */
    function cacheElements() {
        elements = {
            chatButton: shadowRoot.querySelector('.ddp-chat-button'),
            chatPopup: shadowRoot.querySelector('.ddp-chat-popup'),
            closeButton: shadowRoot.querySelector('.ddp-close-button'),
            statusDot: shadowRoot.querySelector('.ddp-status-dot'),
            statusText: shadowRoot.querySelector('.ddp-status-text'),
            handoffBanner: shadowRoot.querySelector('.ddp-handoff-banner'),
            messagesContainer: shadowRoot.querySelector('.ddp-chat-messages'),
            chatInput: shadowRoot.querySelector('.ddp-chat-input'),
            sendButton: shadowRoot.querySelector('.ddp-send-button')
        };
    }

    /**
     * Get cached elements.
     * @returns {Object}
     */
    function getElements() {
        return elements;
    }

    /**
     * Toggle chat popup visibility.
     */
    function togglePopup() {
        const isOpen = elements.chatPopup.classList.toggle('open');
        elements.chatButton.classList.toggle('hidden', isOpen);

        if (isOpen && !elements.chatInput.disabled) {
            elements.chatInput.focus();
        }
    }

    /**
     * Open chat popup.
     */
    function openPopup() {
        elements.chatPopup.classList.add('open');
        elements.chatButton.classList.add('hidden');

        if (!elements.chatInput.disabled) {
            elements.chatInput.focus();
        }
    }

    /**
     * Close chat popup.
     */
    function closePopup() {
        elements.chatPopup.classList.remove('open');
        elements.chatButton.classList.remove('hidden');
    }

    /**
     * Update connection status display.
     * @param {string} status - 'connecting', 'connected', or 'disconnected'
     */
    function updateStatus(status) {
        elements.statusDot.className = 'ddp-status-dot ' + status;

        const messages = {
            connecting: 'Connecting...',
            connected: 'Online',
            disconnected: 'Disconnected'
        };
        elements.statusText.textContent = messages[status] || status;
    }

    /**
     * Enable chat input.
     */
    function enableInput() {
        elements.chatInput.disabled = false;
        elements.sendButton.disabled = false;
        if (elements.chatPopup.classList.contains('open')) {
            elements.chatInput.focus();
        }
    }

    /**
     * Disable chat input.
     */
    function disableInput() {
        elements.chatInput.disabled = true;
        elements.sendButton.disabled = true;
    }

    /**
     * Clear chat input.
     */
    function clearInput() {
        elements.chatInput.value = '';
        elements.chatInput.style.height = 'auto';
    }

    /**
     * Get input value.
     * @returns {string}
     */
    function getInputValue() {
        return elements.chatInput.value;
    }

    /**
     * Auto-resize textarea based on content.
     */
    function autoResizeInput() {
        elements.chatInput.style.height = 'auto';
        elements.chatInput.style.height = Math.min(elements.chatInput.scrollHeight, 100) + 'px';
    }

    /**
     * Add a message to the chat.
     * @param {string} type - 'user', 'bot', 'agent', or 'system'
     * @param {string} content - Message content
     * @param {Object} meta - Optional metadata
     * @returns {HTMLElement} - The message element
     */
    function addMessage(type, content, meta) {
        meta = meta || {};
        hideTypingIndicator();

        const messageDiv = document.createElement('div');
        messageDiv.className = 'ddp-message ' + type;

        const contentDiv = document.createElement('div');
        contentDiv.className = 'ddp-message-content';

        // Render markdown for bot/agent messages
        if (type === 'bot' || type === 'agent') {
            contentDiv.innerHTML = markdownParser.parse(content);
        } else {
            contentDiv.textContent = content;
        }

        messageDiv.appendChild(contentDiv);

        // Add meta info (timestamp, agent name)
        if (type !== 'system') {
            const metaDiv = document.createElement('div');
            metaDiv.className = 'ddp-message-meta';

            const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            metaDiv.textContent = type === 'agent' && meta.agentName
                ? meta.agentName + ' \u2022 ' + time
                : time;

            messageDiv.appendChild(metaDiv);
        }

        elements.messagesContainer.appendChild(messageDiv);
        scrollToBottom();

        return messageDiv;
    }

    /**
     * Add a system message with optional markdown rendering.
     * @param {string} content - Message content
     * @param {boolean} renderMarkdown - Whether to render markdown (default: true)
     */
    function addSystemMessage(content, renderMarkdown) {
        if (renderMarkdown !== false) {
            // Render markdown for system messages (welcome messages, etc.)
            hideTypingIndicator();

            var messageDiv = document.createElement('div');
            messageDiv.className = 'ddp-message system';

            var contentDiv = document.createElement('div');
            contentDiv.className = 'ddp-message-content';
            contentDiv.innerHTML = markdownParser.parse(content);

            messageDiv.appendChild(contentDiv);
            elements.messagesContainer.appendChild(messageDiv);
            scrollToBottom();
        } else {
            addMessage('system', content);
        }
    }

    /**
     * Show typing indicator.
     */
    function showTypingIndicator() {
        hideTypingIndicator();

        const indicator = document.createElement('div');
        indicator.className = 'ddp-message bot';
        indicator.id = 'ddp-typing-indicator';

        const typing = document.createElement('div');
        typing.className = 'ddp-typing-indicator';
        typing.innerHTML = '<span></span><span></span><span></span>';

        indicator.appendChild(typing);
        elements.messagesContainer.appendChild(indicator);
        scrollToBottom();
    }

    /**
     * Hide typing indicator.
     */
    function hideTypingIndicator() {
        const indicator = shadowRoot.getElementById('ddp-typing-indicator');
        if (indicator) {
            indicator.remove();
        }
    }

    /**
     * Append text to streaming message.
     * @param {string} text - Text chunk to append
     */
    function appendToStreamingMessage(text) {
        hideTypingIndicator();

        if (!currentStreamingMessage) {
            const messageDiv = document.createElement('div');
            messageDiv.className = 'ddp-message bot';
            messageDiv.id = 'ddp-streaming-message';

            const contentDiv = document.createElement('div');
            contentDiv.className = 'ddp-message-content';

            messageDiv.appendChild(contentDiv);
            elements.messagesContainer.appendChild(messageDiv);

            currentStreamingMessage = contentDiv;
            currentStreamingText = '';
        }

        currentStreamingText += text;
        currentStreamingMessage.innerHTML = markdownParser.parse(currentStreamingText);
        scrollToBottom();
    }

    /**
     * Finalize streaming message with metadata.
     * @param {Object} payload - Final payload with citations, confidence, etc.
     */
    function finalizeStreamingMessage(payload) {
        if (!currentStreamingMessage) return;

        const messageDiv = currentStreamingMessage.parentElement;

        // Final markdown render
        currentStreamingMessage.innerHTML = markdownParser.parse(currentStreamingText);

        // Add citations
        if (payload.citations && payload.citations.length > 0) {
            const citationsDiv = document.createElement('div');
            citationsDiv.className = 'ddp-citations';

            const header = document.createElement('div');
            header.className = 'ddp-citations-header';
            header.textContent = 'Sources';
            citationsDiv.appendChild(header);

            payload.citations.slice(0, 3).forEach(function(citation) {
                const citationDiv = document.createElement('div');
                citationDiv.className = 'ddp-citation';

                if (citation.url) {
                    const link = document.createElement('a');
                    link.href = citation.url;
                    link.target = '_blank';
                    link.rel = 'noopener noreferrer';
                    link.textContent = citation.source || citation.document_id;
                    citationDiv.appendChild(link);
                } else {
                    citationDiv.textContent = citation.source || citation.document_id;
                }

                citationsDiv.appendChild(citationDiv);
            });

            messageDiv.appendChild(citationsDiv);
        }

        // Add timestamp
        const metaDiv = document.createElement('div');
        metaDiv.className = 'ddp-message-meta';
        metaDiv.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        messageDiv.appendChild(metaDiv);

        // Reset streaming state
        currentStreamingMessage = null;
        currentStreamingText = '';
        scrollToBottom();
    }

    /**
     * Show handoff banner.
     * @param {string} message - Banner message
     */
    function showHandoffBanner(message) {
        elements.handoffBanner.textContent = message;
        elements.handoffBanner.classList.add('visible');
    }

    /**
     * Hide handoff banner.
     */
    function hideHandoffBanner() {
        elements.handoffBanner.classList.remove('visible');
    }

    /**
     * Show handoff confirmation prompt with button.
     */
    function showHandoffConfirmation() {
        hideTypingIndicator();

        const messageDiv = document.createElement('div');
        messageDiv.className = 'ddp-message system ddp-handoff-confirm';

        const contentDiv = document.createElement('div');
        contentDiv.className = 'ddp-message-content';
        contentDiv.innerHTML = '<p>Would you like to speak with a human agent?</p>';

        const button = document.createElement('button');
        button.className = 'ddp-handoff-button';
        button.textContent = 'Yes, connect me to a human';
        button.addEventListener('click', function() {
            // Remove the confirmation message
            messageDiv.remove();
            // Send confirmation to initiate handoff
            DDPWebSocket.send({
                type: 'confirm_handoff',
                payload: {}
            });
            // Show connecting message
            addSystemMessage('Connecting you with a human agent...');
            showHandoffBanner('\uD83E\uDD1D Connecting you with a human agent...');
        });

        contentDiv.appendChild(button);
        messageDiv.appendChild(contentDiv);
        elements.messagesContainer.appendChild(messageDiv);
        scrollToBottom();
    }

    /**
     * Scroll messages to bottom.
     */
    function scrollToBottom() {
        elements.messagesContainer.scrollTop = elements.messagesContainer.scrollHeight;
    }

    /**
     * Handle UI update events from chat module.
     * @param {Object} event - UI event
     */
    function handleUIUpdate(event) {
        switch (event.type) {
            case 'user_message':
                addMessage('user', event.payload.message);
                clearInput();
                break;

            case 'restored_message':
                addMessage(
                    event.payload.role === 'user' ? 'user' : 'bot',
                    event.payload.content
                );
                break;

            case 'typing_show':
                showTypingIndicator();
                break;

            case 'stream_chunk':
                appendToStreamingMessage(event.payload.text);
                break;

            case 'stream_end':
                finalizeStreamingMessage(event.payload);
                break;

            case 'input_enable':
                enableInput();
                break;

            case 'input_disable':
                disableInput();
                break;

            case 'handoff_suggested':
                showHandoffConfirmation();
                break;

            case 'handoff_requested':
                addSystemMessage('Connecting you with a human agent...');
                showHandoffBanner('\uD83E\uDD1D Connecting you with a human agent...');
                break;

            case 'agent_joined':
                showHandoffBanner('\uD83E\uDD1D Connected to ' + event.payload.agent_name);
                addSystemMessage(event.payload.agent_name + ' has joined the conversation.');
                break;

            case 'agent_message':
                addMessage('agent', event.payload.text, {
                    agentName: event.payload.agent_name
                });
                break;

            case 'agent_left':
                hideHandoffBanner();
                addSystemMessage('The agent has ended the conversation. You\'re now chatting with VoteBot again.');
                enableInput();
                break;

            case 'error':
                hideTypingIndicator();
                addSystemMessage('Error: ' + event.payload.message);
                break;

            case 'system_message':
                addSystemMessage(event.payload.message);
                break;
        }
    }

    return {
        init: init,
        buildHTML: buildHTML,
        cacheElements: cacheElements,
        getElements: getElements,
        togglePopup: togglePopup,
        openPopup: openPopup,
        closePopup: closePopup,
        updateStatus: updateStatus,
        enableInput: enableInput,
        disableInput: disableInput,
        clearInput: clearInput,
        getInputValue: getInputValue,
        autoResizeInput: autoResizeInput,
        addMessage: addMessage,
        addSystemMessage: addSystemMessage,
        showTypingIndicator: showTypingIndicator,
        hideTypingIndicator: hideTypingIndicator,
        appendToStreamingMessage: appendToStreamingMessage,
        finalizeStreamingMessage: finalizeStreamingMessage,
        showHandoffBanner: showHandoffBanner,
        hideHandoffBanner: hideHandoffBanner,
        showHandoffConfirmation: showHandoffConfirmation,
        scrollToBottom: scrollToBottom,
        handleUIUpdate: handleUIUpdate
    };
})();
