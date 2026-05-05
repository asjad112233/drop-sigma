import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async


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


class ChatConsumer(AsyncWebsocketConsumer):
    """
    Shared real-time chat consumer for admin, employee and vendor portals.
    All three portals connect to the same channel rooms — messages are visible
    across portals because they share the same ChatChannel/ChatMessage models.
    """

    async def connect(self):
        if not self.scope["user"].is_authenticated:
            await self.close()
            return

        self.channel_id = self.scope["url_route"]["kwargs"]["channel_id"]
        self.room_group = f"chat_{self.channel_id}"
        self.display_name = await database_sync_to_async(_resolve_display_name)(self.scope["user"])
        self.user_role = await database_sync_to_async(_resolve_user_role)(self.scope["user"])

        await self.channel_layer.group_add(self.room_group, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if hasattr(self, "room_group"):
            # Let others know this user stopped typing
            await self.channel_layer.group_send(self.room_group, {
                "type": "typing_event",
                "user_id": self.scope["user"].id,
                "username": self.display_name,
                "typing": False,
            })
            await self.channel_layer.group_discard(self.room_group, self.channel_name)

    async def receive(self, text_data):
        data = json.loads(text_data)
        event = data.get("type")
        user = self.scope["user"]

        if event == "typing":
            await self.channel_layer.group_send(self.room_group, {
                "type": "typing_event",
                "user_id": user.id,
                "username": self.display_name,
                "typing": data.get("typing", False),
            })

        elif event == "new_message":
            content = data.get("content", "")
            preview = content[:80] if content else ""
            await self.channel_layer.group_send(self.room_group, {
                "type": "message_event",
                "channel_id": int(self.channel_id),
                "sender_id": user.id,
                "sender_name": self.display_name,
                "sender_role": self.user_role,
                "preview": preview,
            })

    # ── Group event handlers ──────────────────────────────────────────────────

    async def typing_event(self, event):
        if event["user_id"] == self.scope["user"].id:
            return  # don't echo back to sender
        await self.send(text_data=json.dumps({
            "type": "typing",
            "username": event["username"],
            "typing": event["typing"],
        }))

    async def message_event(self, event):
        await self.send(text_data=json.dumps({
            "type": "new_message",
            "channel_id": event["channel_id"],
            "sender_name": event["sender_name"],
            "sender_role": event.get("sender_role", "member"),
            "preview": event.get("preview", ""),
        }))
