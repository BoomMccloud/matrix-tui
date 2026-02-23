"""Matrix bot — bridges room messages to the agent, manages room/container lifecycle."""

import io
import logging

from nio import (
    AsyncClient,
    InviteMemberEvent,
    LoginResponse,
    RoomMemberEvent,
    RoomMessageText,
    UploadResponse,
)

from .agent import Agent
from .config import Settings
from .sandbox import SandboxManager

log = logging.getLogger(__name__)


class Bot:
    def __init__(self, settings: Settings, sandbox: SandboxManager, agent: Agent):
        self.settings = settings
        self.sandbox = sandbox
        self.agent = agent
        self.client = AsyncClient(settings.matrix_homeserver, settings.matrix_user)
        self._synced = False

    async def _login(self):
        resp = await self.client.login(self.settings.matrix_password)
        if not isinstance(resp, LoginResponse):
            raise RuntimeError(f"Login failed: {resp}")
        log.info("Logged in as %s", self.settings.matrix_user)

    async def _on_invite(self, room, event):
        """Join room on invite. No container yet — that happens on first message."""
        if not self._synced:
            log.info("_on_invite SKIPPED (pre-sync) for %s by %s", room.room_id, event.sender)
            return
        log.info("_on_invite FIRED for %s by %s", room.room_id, event.sender)
        await self.client.join(room.room_id)
        await self.client.room_send(
            room.room_id, "m.room.message",
            {"msgtype": "m.text", "body": "[invite] Ready! Send me a task to get started."},
        )

    async def _on_message(self, room, event):
        """Handle text messages. Creates sandbox on first message if needed."""
        if event.sender == self.client.user_id:
            return
        if not self._synced:
            return

        room_id = room.room_id
        text = event.body

        # Create sandbox on first message
        if room_id not in self.sandbox._containers:
            log.info("First message in %s — creating sandbox", room_id)
            try:
                await self.sandbox.create(room_id)
            except Exception as e:
                log.exception("Failed to create sandbox for %s", room_id)
                await self.client.room_send(
                    room_id, "m.room.message",
                    {"msgtype": "m.text", "body": f"Failed to create sandbox: {e}"},
                )
                return

        try:
            async for reply_text, image in self.agent.handle_message(room_id, text):
                if image:
                    await self._send_image(room_id, image)
                if reply_text:
                    await self.client.room_send(
                        room_id, "m.room.message",
                        {"msgtype": "m.text", "body": reply_text},
                    )
        except Exception as e:
            log.exception("Agent error in %s", room_id)
            await self.client.room_send(
                room_id, "m.room.message",
                {"msgtype": "m.text", "body": f"Error: {e}"},
            )

    async def _on_member(self, room, event):
        """Cleanup when bot is kicked or last user leaves."""
        if not self._synced:
            return

        if event.state_key == self.client.user_id and event.membership in ("leave", "ban"):
            log.info("Bot removed from %s — destroying sandbox", room.room_id)
            await self.sandbox.destroy(room.room_id)
            return

        if event.membership in ("leave", "ban") and event.state_key != self.client.user_id:
            non_bot = [u for u in room.users if u != self.client.user_id]
            if not non_bot:
                log.info("All users left %s — destroying sandbox and leaving", room.room_id)
                await self.sandbox.destroy(room.room_id)
                await self.client.room_leave(room.room_id)

    async def _send_image(self, room_id: str, image_data: bytes):
        """Upload and send an image to a room."""
        resp, _ = await self.client.upload(
            io.BytesIO(image_data),
            content_type="image/png",
            filename="screenshot.png",
            filesize=len(image_data),
        )
        if not isinstance(resp, UploadResponse):
            log.error("Image upload failed: %s", resp)
            return
        await self.client.room_send(
            room_id, "m.room.message",
            {
                "msgtype": "m.image",
                "body": "screenshot.png",
                "url": resp.content_uri,
                "info": {"mimetype": "image/png", "size": len(image_data)},
            },
        )

    async def _reconnect_containers(self):
        """Reconnect to existing sandbox containers from a previous run."""
        rc, out, err = await self.sandbox._run("ps", "--format", "{{.Names}}", "--filter", "ancestor=" + self.settings.sandbox_image)
        if rc != 0 or not out.strip():
            return
        for name in out.strip().splitlines():
            # We don't have room_id -> container name mapping yet,
            # so just log what's running. Future: use named containers.
            log.info("Found existing container: %s", name)

    async def run(self):
        await self._login()

        self.client.add_event_callback(self._on_invite, InviteMemberEvent)
        self.client.add_event_callback(self._on_message, RoomMessageText)
        self.client.add_event_callback(self._on_member, RoomMemberEvent)

        await self.client.sync(timeout=10000)

        # Auto-join pending invites from before startup (no greeting — stale invites)
        for room_id in list(self.client.invited_rooms):
            log.info("catch-up join (no greeting) for %s", room_id)
            await self.client.join(room_id)

        await self._reconnect_containers()

        self._synced = True
        log.info("Initial sync complete, now listening")

        try:
            await self.client.sync_forever(timeout=30000)
        finally:
            log.info("Shutting down — destroying all sandboxes")
            await self.sandbox.destroy_all()
            await self.client.close()
