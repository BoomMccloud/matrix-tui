"""Matrix bot — channel adapter bridging room messages to the decider."""

import asyncio
import io
import json
import logging
import os

from nio import (
    AsyncClient,
    InviteMemberEvent,
    LoginResponse,
    RoomMemberEvent,
    RoomMessageText,
    SyncResponse,
    UploadResponse,
)

from .decider import Decider, SYSTEM_PROMPT
from .config import Settings
from .sandbox import SandboxManager
from .channels import ChannelAdapter
from .core import TaskRunner

log = logging.getLogger(__name__)


class MatrixChannel(ChannelAdapter):
    system_prompt = SYSTEM_PROMPT

    def __init__(self, bot: "Bot", room_id: str):
        self.bot = bot
        self.room_id = room_id

    async def start(self) -> None:
        pass  # Matrix client lifecycle is managed by Bot.run()

    async def stop(self) -> None:
        pass

    async def send_update(self, task_id: str, text: str) -> None:
        """Send intermediate output as a Matrix message."""
        content = {"msgtype": "m.text", "body": f"```\n{text}\n```"}
        await self.bot.client.room_send(
            self.room_id, "m.room.message", content
        )

    async def deliver_result(self, task_id: str, text: str) -> None:
        content = {"msgtype": "m.text", "body": text}
        await self.bot.client.room_send(
            self.room_id, "m.room.message", content
        )

    async def deliver_error(self, task_id: str, error: str) -> None:
        content = {"msgtype": "m.text", "body": f"Error: {error}"}
        await self.bot.client.room_send(
            self.room_id, "m.room.message", content
        )

    async def is_valid(self, task_id: str) -> bool:
        return task_id in self.bot.client.rooms


class Bot:
    def __init__(self, settings: Settings, sandbox: SandboxManager, decider: Decider, task_runner: TaskRunner):
        self.settings = settings
        self.sandbox = sandbox
        self.decider = decider
        self.task_runner = task_runner
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
        if event.state_key != self.client.user_id:
            return
        log.info("_on_invite FIRED for %s by %s", room.room_id, event.sender)
        await self.client.join(room.room_id)
        await self.client.room_send(
            room.room_id, "m.room.message",
            {"msgtype": "m.text", "body": "[invite] Ready! Send me a task to get started."},
        )

    async def _on_message(self, room, event):
        """Enqueue incoming messages for per-room processing."""
        if event.sender == self.client.user_id:
            return
        if not self._synced:
            return
        log.info("Message from %s in %s: %s", event.sender, room.room_id, event.body[:80])

        channel = MatrixChannel(self, room.room_id)
        await self.task_runner.enqueue(room.room_id, event.body, channel)

    async def _on_member(self, room, event):
        """Cleanup when bot is kicked or last user leaves."""
        if not self._synced:
            return
        log.info("Member event in %s: %s -> %s (state_key=%s)",
                 room.room_id, event.prev_membership, event.membership, event.state_key)

        if event.state_key == self.client.user_id and event.membership in ("leave", "ban"):
            log.info("Bot removed from %s — destroying sandbox", room.room_id)
            await self.task_runner._cleanup(room.room_id)
            return

        if event.membership in ("leave", "ban") and event.state_key != self.client.user_id:
            non_bot = [u for u in room.users if u != self.client.user_id]
            if not non_bot:
                log.info("All users left %s — destroying sandbox and leaving", room.room_id)
                await self.task_runner._cleanup(room.room_id)
                await self.client.room_leave(room.room_id)

    async def _watch_ipc(self, room_id: str, container_name: str) -> None:
        """Poll for IPC files (notification, progress, result) and send Matrix messages."""
        ipc_dir = os.path.join(self.settings.ipc_base_dir, container_name)
        try:
            while True:
                await asyncio.sleep(1)
                for filename, formatter in (
                    ("notification.json", self._format_notification),
                    ("event-progress.json", self._format_progress),
                    ("event-result.json", self._format_result),
                ):
                    filepath = os.path.join(ipc_dir, filename)
                    if not os.path.exists(filepath):
                        continue
                    try:
                        with open(filepath) as f:
                            data = json.load(f)
                        log.info("[%s] IPC %s: %s", container_name, filename, json.dumps(data)[:300])
                        body = formatter(data)
                    except Exception:
                        log.exception("[%s] Failed to parse IPC %s", container_name, filename)
                        body = f"⚠️ IPC event ({filename}, could not parse)"
                    os.unlink(filepath)
                    await self.client.room_send(
                        room_id, "m.room.message",
                        {"msgtype": "m.text", "body": body},
                    )
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _format_notification(data: dict) -> str:
        ntype = data.get("notification_type", "unknown")
        message = data.get("message", "")
        details = data.get("details", {})
        body = f"⚠️ Gemini [{ntype}]: {message}"
        if details:
            body += f"\nDetails: {json.dumps(details, indent=2)}"
        return body

    @staticmethod
    def _format_progress(data: dict) -> str:
        tool_name = data.get("tool_name", data.get("name", "unknown"))
        return f"🔧 Tool completed: {tool_name}"

    @staticmethod
    def _format_result(data: dict) -> str:
        cli = data.get("cli", "gemini")
        exit_code = data.get("exit_code", "?")
        return f"✅ Agent finished ({cli}, exit {exit_code})"

    async def _keep_typing(self, room_id: str) -> None:
        """Send typing indicator every 20s until cancelled."""
        try:
            while True:
                await self.client.room_typing(room_id, typing_state=True, timeout=30000)
                await asyncio.sleep(20)
        except asyncio.CancelledError:
            pass

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

    async def run(self):
        await self._login()

        self.client.add_event_callback(self._on_invite, InviteMemberEvent)
        self.client.add_event_callback(self._on_message, RoomMessageText)
        self.client.add_event_callback(self._on_member, RoomMemberEvent)

        log.info("Starting initial sync...")
        resp = await self.client.sync(timeout=10000)
        log.info("Initial sync result: %s", type(resp).__name__)

        # Auto-join pending invites from before startup (no greeting — stale invites)
        for room_id in list(self.client.invited_rooms):
            log.info("catch-up join (no greeting) for %s", room_id)
            await self.client.join(room_id)

        # State + histories already loaded in __main__.py
        self._synced = True
        log.info("Initial sync complete, now listening")

        async def on_sync(response):
            log.info("Sync OK: next_batch=%s", response.next_batch)

        self.client.add_response_callback(on_sync, SyncResponse)

        reconcile_task = asyncio.create_task(self.task_runner.reconcile_loop())
        try:
            await self.client.sync_forever(timeout=30000)
        finally:
            reconcile_task.cancel()
            log.info("Shutting down")
            await self.client.close()
