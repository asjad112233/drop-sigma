import json
import logging

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async

logger = logging.getLogger(__name__)


def _resolve_display_name(user):
    """Return the best display name for any user type (admin/team/vendor)."""
    try:
        profile = user.team_profile.first()
        if profile:
            return profile.name
    except Exception:
        pass
    try:
        vendor = user.vendor_profile
        if vendor:
            return vendor.name
    except Exception:
        pass
    return user.get_full_name() or user.username


def _resolve_user_role(user):
    """Return a role string for UI display."""
    if user.is_superuser or user.is_staff:
        return "owner"
    try:
        profile = user.team_profile.first()
        if profile:
            return profile.role
    except Exception:
        pass
    try:
        _ = user.vendor_profile
        return "vendor"
    except Exception:
        pass
    return "member"


def _user_can_access_channel_sync(user, channel_id):
    """Sync check used inside database_sync_to_async — mirrors the HTTP
    helper but takes a channel_id so we don't have to ship the channel
    object across the wire."""
    from .models import ChatChannel, ChannelMember
    if not user or not user.is_authenticated:
        return False
    try:
        ch = ChatChannel.objects.get(id=channel_id)
    except ChatChannel.DoesNotExist:
        return False
    if user.is_superuser:
        return True
    if ch.is_dm:
        return ch.participants.filter(id=user.id).exists()
    return ChannelMember.objects.filter(channel=ch, user=user, is_active=True).exists()


class ChatConsumer(AsyncWebsocketConsumer):
    """
    Shared real-time chat consumer for admin, employee and vendor portals.
    All three portals connect to the same channel rooms — messages are visible
    across portals because they share the same ChatChannel/ChatMessage models.

    On connect we enforce membership of the requested channel to prevent
    cross-tenant eavesdropping over the WS broadcast group.
    """

    async def connect(self):
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            await self.close(code=4401)
            return

        try:
            channel_id = int(self.scope["url_route"]["kwargs"]["channel_id"])
        except (TypeError, ValueError, KeyError):
            await self.close(code=4400)
            return

        # Verify channel membership BEFORE joining the broadcast group.
        allowed = await database_sync_to_async(_user_can_access_channel_sync)(user, channel_id)
        if not allowed:
            await self.close(code=4403)
            return

        self.channel_id = channel_id
        self.room_group = f"chat_{channel_id}"
        try:
            self.display_name = await database_sync_to_async(_resolve_display_name)(user)
            self.user_role    = await database_sync_to_async(_resolve_user_role)(user)
        except Exception:
            logger.exception("chat-ws: failed to resolve user identity for uid=%s", getattr(user, "id", None))
            self.display_name = user.username if hasattr(user, "username") else "user"
            self.user_role    = "member"

        await self.channel_layer.group_add(self.room_group, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "room_group"):
            user = self.scope.get("user")
            if user and getattr(user, "is_authenticated", False):
                try:
                    # Let others know this user stopped typing
                    await self.channel_layer.group_send(self.room_group, {
                        "type": "typing_event",
                        "user_id": user.id,
                        "username": getattr(self, "display_name", user.username),
                        "typing": False,
                    })
                except Exception:
                    logger.exception("chat-ws: failed to emit final typing-off for uid=%s", user.id)
            try:
                await self.channel_layer.group_discard(self.room_group, self.channel_name)
            except Exception:
                logger.exception("chat-ws: failed to discard group %s", self.room_group)

    async def receive(self, text_data):
        # Be tolerant of malformed payloads — never crash the consumer.
        try:
            data = json.loads(text_data or "{}")
        except (ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return

        event = data.get("type")
        user = self.scope.get("user")
        if not user or not user.is_authenticated:
            return

        if event == "typing":
            try:
                await self.channel_layer.group_send(self.room_group, {
                    "type": "typing_event",
                    "user_id": user.id,
                    "username": self.display_name,
                    "typing": bool(data.get("typing", False)),
                })
            except Exception:
                logger.exception("chat-ws: typing broadcast failed for uid=%s", user.id)
        # Note: clients MUST NOT send "new_message" over WS — actual message
        # persistence + broadcast happens via the HTTP chat/send/ endpoint
        # (which calls _broadcast_chat_message). Any client-driven
        # "new_message" payloads are intentionally ignored here to prevent
        # spoofed broadcasts.

    # ── Group event handlers ──────────────────────────────────────────────────

    async def typing_event(self, event):
        if event.get("user_id") == self.scope["user"].id:
            return  # don't echo back to sender
        try:
            await self.send(text_data=json.dumps({
                "type": "typing",
                "username": event.get("username", ""),
                "typing": bool(event.get("typing", False)),
            }))
        except Exception:
            logger.exception("chat-ws: failed to send typing_event")

    async def message_event(self, event):
        try:
            await self.send(text_data=json.dumps({
                "type":        "new_message",
                "channel_id":  event.get("channel_id"),
                "message_id":  event.get("message_id"),
                "sender_id":   event.get("sender_id"),
                "sender_name": event.get("sender_name", ""),
                "sender_role": event.get("sender_role", "member"),
                "preview":     event.get("preview", ""),
            }))
        except Exception:
            logger.exception("chat-ws: failed to send message_event")
