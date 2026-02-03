"""Slack integration service for human handoff support."""

import asyncio
from typing import Callable, Optional

import structlog
from slack_sdk.web.async_client import AsyncWebClient
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from votebot.config import get_settings

logger = structlog.get_logger()


class SlackService:
    """
    Handles Slack integration for human agent handoff.

    Uses Socket Mode for real-time event handling without webhooks.
    """

    def __init__(self):
        self.settings = get_settings()
        self._web_client: Optional[AsyncWebClient] = None
        self._socket_client: Optional[SocketModeClient] = None
        self._running = False
        self._agent_message_callback: Optional[Callable] = None
        self._handoff_resolved_callback: Optional[Callable] = None

    @property
    def is_configured(self) -> bool:
        """Check if Slack credentials are configured."""
        bot_token = self.settings.slack_bot_token.get_secret_value()
        app_token = self.settings.slack_app_token.get_secret_value()
        return bool(bot_token and app_token)

    async def start(
        self,
        on_agent_message: Callable[[str, str, str], None],
        on_handoff_resolved: Callable[[str], None],
    ):
        """
        Start the Slack Socket Mode client.

        Args:
            on_agent_message: Callback(thread_ts, agent_name, message) when agent replies
            on_handoff_resolved: Callback(thread_ts) when handoff is resolved (checkmark reaction)
        """
        if not self.is_configured:
            logger.warning("Slack credentials not configured, skipping Slack integration")
            return

        self._agent_message_callback = on_agent_message
        self._handoff_resolved_callback = on_handoff_resolved

        bot_token = self.settings.slack_bot_token.get_secret_value()
        app_token = self.settings.slack_app_token.get_secret_value()

        self._web_client = AsyncWebClient(token=bot_token)
        self._socket_client = SocketModeClient(
            app_token=app_token,
            web_client=self._web_client,
        )

        # Register event handler
        self._socket_client.socket_mode_request_listeners.append(
            self._handle_socket_event
        )

        self._running = True
        await self._socket_client.connect()
        logger.info("Slack Socket Mode client connected")

    async def stop(self):
        """Stop the Slack Socket Mode client."""
        self._running = False
        if self._socket_client:
            await self._socket_client.close()
            self._socket_client = None
        self._web_client = None
        logger.info("Slack Socket Mode client disconnected")

    async def _handle_socket_event(
        self,
        client: SocketModeClient,
        request: SocketModeRequest,
    ):
        """Handle incoming Socket Mode events."""
        # Log all incoming events for debugging
        logger.info(
            "Socket Mode event received",
            request_type=request.type,
            payload_keys=list(request.payload.keys()) if request.payload else [],
        )

        # Acknowledge the event
        response = SocketModeResponse(envelope_id=request.envelope_id)
        await client.send_socket_mode_response(response)

        if request.type == "events_api":
            event = request.payload.get("event", {})
            event_type = event.get("type")

            logger.info(
                "Processing Slack event",
                event_type=event_type,
                channel=event.get("channel"),
                thread_ts=event.get("thread_ts"),
                user=event.get("user"),
            )

            if event_type == "message":
                await self._handle_message_event(event)
            elif event_type == "reaction_added":
                await self._handle_reaction_event(event)

    async def _handle_message_event(self, event: dict):
        """Handle message events (agent replies in threads)."""
        # Only handle thread replies
        thread_ts = event.get("thread_ts")
        if not thread_ts:
            return

        # Ignore bot messages
        if event.get("bot_id"):
            return

        # Ignore subtypes like message_changed
        if event.get("subtype"):
            return

        user_id = event.get("user")
        text = event.get("text", "")
        channel = event.get("channel")

        # Verify this is in our support channel
        if channel != await self._get_support_channel_id():
            return

        # Get user info for display name
        agent_name = await self._get_user_name(user_id)

        logger.info(
            "Agent message received",
            thread_ts=thread_ts,
            agent=agent_name,
            message_preview=text[:50],
        )

        if self._agent_message_callback:
            await self._agent_message_callback(thread_ts, agent_name, text)

    async def _handle_reaction_event(self, event: dict):
        """Handle reaction events (checkmark to resolve)."""
        reaction = event.get("reaction")
        item = event.get("item", {})

        # Check for resolve reaction (white_check_mark or heavy_check_mark)
        if reaction not in ("white_check_mark", "heavy_check_mark"):
            return

        # Only handle reactions on messages
        if item.get("type") != "message":
            return

        thread_ts = item.get("ts")
        channel = item.get("channel")

        # Verify this is in our support channel
        if channel != await self._get_support_channel_id():
            return

        logger.info("Handoff resolved via reaction", thread_ts=thread_ts)

        if self._handoff_resolved_callback:
            await self._handoff_resolved_callback(thread_ts)

    async def _get_support_channel_id(self) -> Optional[str]:
        """Get the channel ID for the support channel."""
        if not self._web_client:
            return None

        # Cache the channel ID after first lookup
        if hasattr(self, "_support_channel_id_cache") and self._support_channel_id_cache:
            return self._support_channel_id_cache

        channel_name = self.settings.slack_support_channel.lstrip("#")

        try:
            # Query public and private channels separately (combined query has issues)
            all_channels = []

            # Get public channels
            result = await self._web_client.conversations_list(
                types="public_channel",
                exclude_archived=True,
            )
            all_channels.extend(result.get("channels", []))

            # Get private channels (requires groups:read scope)
            try:
                result = await self._web_client.conversations_list(
                    types="private_channel",
                    exclude_archived=True,
                )
                all_channels.extend(result.get("channels", []))
            except Exception as e:
                logger.debug("Could not list private channels", error=str(e))

            for channel in all_channels:
                if channel.get("name") == channel_name:
                    channel_id = channel.get("id")
                    self._support_channel_id_cache = channel_id
                    logger.info(
                        "Found support channel",
                        channel_name=channel_name,
                        channel_id=channel_id,
                    )
                    return channel_id

            # Log available channels for debugging
            available = [c.get("name") for c in all_channels]
            logger.warning(
                "Support channel not found",
                looking_for=channel_name,
                available_channels=available[:15],
            )
        except Exception as e:
            logger.error("Failed to get support channel ID", error=str(e))

        self._support_channel_id_cache = None
        return None

    async def _get_user_name(self, user_id: str) -> str:
        """Get display name for a user."""
        if not self._web_client:
            logger.warning("No Slack web client available for user lookup")
            return "Agent"

        try:
            result = await self._web_client.users_info(user=user_id)
            user = result.get("user", {})
            profile = user.get("profile", {})

            display_name = profile.get("display_name")
            real_name = profile.get("real_name")
            username = user.get("name")

            logger.info(
                "Slack user info retrieved",
                user_id=user_id,
                display_name=display_name,
                real_name=real_name,
                username=username,
            )

            name = display_name or real_name or username or "Agent"
            return name
        except Exception as e:
            logger.error("Failed to get user name", user_id=user_id, error=str(e))
            return "Agent"

    async def create_handoff_thread(
        self,
        session_id: str,
        page_context: dict,
        latest_message: str,
        conversation_history: list[dict],
    ) -> Optional[str]:
        """
        Create a new handoff thread in the support channel.

        Args:
            session_id: Chat session ID
            page_context: Page context (type, id, title, jurisdiction, url)
            latest_message: The user's latest message
            conversation_history: Recent conversation history

        Returns:
            Thread timestamp (thread_ts) if successful, None otherwise
        """
        if not self._web_client:
            logger.warning("Slack client not initialized")
            return None

        channel_id = await self._get_support_channel_id()
        if not channel_id:
            logger.error("Support channel not found")
            return None

        # Format the handoff message
        page_type = page_context.get("type", "general").title()
        page_title = page_context.get("title", "N/A")
        page_id = page_context.get("id", "")
        jurisdiction = page_context.get("jurisdiction", "")
        page_url = page_context.get("url", "")

        # Build page info line
        page_info_parts = [f"*Page Type:* {page_type}"]
        if jurisdiction:
            page_info_parts.append(f"*Jurisdiction:* {jurisdiction}")
        page_info = "  |  ".join(page_info_parts)

        # Build page title with optional link
        if page_title != "N/A":
            if page_url:
                page_line = f"*Page:* <{page_url}|{page_title}>"
            else:
                page_line = f"*Page:* {page_title}"
            if page_id:
                page_line += f" ({page_id})"
        else:
            page_line = ""

        # Format recent conversation (last 5 messages)
        recent_messages = conversation_history[-5:] if conversation_history else []
        conversation_text = ""
        for msg in recent_messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")[:200]  # Truncate long messages
            if role == "user":
                conversation_text += f":bust_in_silhouette: *Visitor:* {content}\n"
            else:
                conversation_text += f":robot_face: *Bot:* {content}\n"

        # Build the message blocks
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": ":sos: Human Assistance Requested",
                    "emoji": True,
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Session:* `{session_id}`\n{page_info}",
                },
            },
        ]

        if page_line:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": page_line},
            })

        blocks.extend([
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Latest Message:*\n>{latest_message}",
                },
            },
        ])

        if conversation_text:
            blocks.extend([
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Recent Conversation:*\n{conversation_text}",
                    },
                },
            ])

        blocks.extend([
            {"type": "divider"},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": ":bulb: Reply in thread to respond  |  :white_check_mark: to resolve",
                    }
                ],
            },
        ])

        try:
            result = await self._web_client.chat_postMessage(
                channel=channel_id,
                text=f":sos: Human assistance requested - Session: {session_id}",
                blocks=blocks,
            )

            thread_ts = result.get("ts")
            logger.info(
                "Handoff thread created",
                session_id=session_id,
                thread_ts=thread_ts,
            )
            return thread_ts

        except Exception as e:
            logger.error("Failed to create handoff thread", error=str(e))
            return None

    async def relay_user_message(self, thread_ts: str, message: str) -> bool:
        """
        Relay a user message to an existing handoff thread.

        Args:
            thread_ts: Thread timestamp
            message: User's message

        Returns:
            True if successful
        """
        if not self._web_client:
            return False

        channel_id = await self._get_support_channel_id()
        if not channel_id:
            return False

        try:
            await self._web_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f":bust_in_silhouette: *Visitor:* {message}",
            )
            logger.debug("User message relayed to Slack", thread_ts=thread_ts)
            return True

        except Exception as e:
            logger.error("Failed to relay user message", error=str(e))
            return False

    async def send_handoff_resolved_message(self, thread_ts: str) -> bool:
        """
        Send a message indicating the handoff was resolved.

        Args:
            thread_ts: Thread timestamp

        Returns:
            True if successful
        """
        if not self._web_client:
            return False

        channel_id = await self._get_support_channel_id()
        if not channel_id:
            return False

        try:
            await self._web_client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=":white_check_mark: *Handoff resolved* - User returned to VoteBot",
            )
            return True

        except Exception as e:
            logger.error("Failed to send resolved message", error=str(e))
            return False


# Singleton instance
_slack_service: Optional[SlackService] = None


def get_slack_service() -> SlackService:
    """Get the singleton SlackService instance."""
    global _slack_service
    if _slack_service is None:
        _slack_service = SlackService()
    return _slack_service
