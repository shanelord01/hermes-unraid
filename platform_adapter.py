"""Unraid platform adapter: alerts in, notifications out.

Inbound: subscribes to the Unraid API's ``notificationsWarningsAndAlerts``
GraphQL subscription over graphql-transport-ws and forwards new warnings and
alerts to the agent.

Outbound: delivers agent messages as Unraid notifications via
``createNotification``, so they appear in the webGUI notification centre and
flow through whatever notification agents Unraid has configured.

Two things shape the design:

* ``notificationsWarningsAndAlerts`` returns a **list**, not a single event. It
  re-sends the whole current set whenever anything changes, so without dedupe
  by notification id every new alert would re-announce all the old ones.
* Every forwarded event can wake the agent, which costs model tokens. Alerts
  are therefore filtered by importance and rate-limited by default rather than
  opt-in.
"""

import asyncio
import json
import logging
import os
import ssl
import time
from datetime import datetime
from typing import Any, Dict, Optional, Set

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - surfaced through check_requirements
    AIOHTTP_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

_SUBSCRIPTION = """
subscription {
  notificationsWarningsAndAlerts {
    id
    title
    subject
    description
    importance
    timestamp
    link
  }
}
"""

_IMPORTANCE_RANK = {"INFO": 0, "WARNING": 1, "ALERT": 2}
_DEFAULT_MIN_IMPORTANCE = "WARNING"
_DEFAULT_COOLDOWN_SECONDS = 300
_DEFAULT_MAX_PER_HOUR = 20

# Bound the dedupe set so a long-lived gateway cannot grow it without limit.
_SEEN_MAX = 500

_WS_PROTOCOL = "graphql-transport-ws"
_ACK_TIMEOUT_SECONDS = 20
_RECONNECT_BASE_SECONDS = 5
_RECONNECT_MAX_SECONDS = 300


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _settings() -> Dict[str, Any]:
    """Effective alert settings, shared with the tools half and the dashboard."""
    try:
        from . import settings as _s
    except ImportError:  # standalone import for testing
        import settings as _s
    return _s.load()


def _ssl_context() -> ssl.SSLContext:
    # Matches the tools plugin: Unraid's cert is issued for its myunraid.net
    # hostname, so a raw LAN IP endpoint cannot validate.
    ctx = ssl.create_default_context()
    if _env("UNRAID_API_VERIFY_TLS") != "1":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _ws_url() -> str:
    url = _env("UNRAID_API_URL")
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):]
    if url.startswith("http://"):
        return "ws://" + url[len("http://"):]
    return url


def check_requirements() -> bool:
    return AIOHTTP_AVAILABLE


def validate_config(config: PlatformConfig) -> bool:
    return bool(_env("UNRAID_API_URL") and _env("UNRAID_API_KEY"))


def _is_connected(config) -> bool:
    return bool(_env("UNRAID_API_URL") and _env("UNRAID_API_KEY"))


class UnraidAdapter(BasePlatformAdapter):
    """Unraid notification adapter: alerts in, notifications out."""

    MAX_MESSAGE_LENGTH = 4000

    def __init__(self, config: PlatformConfig):
        # Platform is a closed enum; _missing_ mints a pseudo-member only for a
        # platform the registry already knows about. That holds once
        # register_platform() has run, so the lookup must happen here in the
        # factory path rather than at import time.
        super().__init__(config=config, platform=Platform("unraid"))
        self._session: Optional[Any] = None
        self._ws: Optional[Any] = None
        self._task: Optional[asyncio.Task] = None
        self._closing = False
        self._seen: Set[str] = set()
        self._seen_order: list = []
        self._last_emit: Dict[str, float] = {}
        self._emitted_times: list = []
        # First payload after connecting is the current backlog, not news.
        # Emitting it would announce every pre-existing alert on every gateway
        # restart, so it seeds the dedupe set instead.
        self._primed = False

    # -- lifecycle ---------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not AIOHTTP_AVAILABLE:
            self._set_fatal_error("missing_dependency", "aiohttp is not installed", retryable=False)
            return False
        if not validate_config(self.config):
            self._set_fatal_error(
                "missing_config", "UNRAID_API_URL and UNRAID_API_KEY must be set", retryable=False
            )
            return False
        self._closing = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._closing = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._cleanup()
        self._mark_disconnected()

    async def _cleanup(self) -> None:
        for obj in (self._ws, self._session):
            try:
                if obj is not None and not getattr(obj, "closed", True):
                    await obj.close()
            except Exception:  # noqa: BLE001
                pass
        self._ws = None
        self._session = None

    # -- inbound -----------------------------------------------------------

    async def _run(self) -> None:
        """Maintain the subscription, reconnecting with capped backoff."""
        backoff = _RECONNECT_BASE_SECONDS
        while not self._closing:
            try:
                await self._subscribe_forever()
                backoff = _RECONNECT_BASE_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - the loop must survive anything
                logger.warning("[Unraid] subscription dropped: %s", e)
            finally:
                await self._cleanup()
            if self._closing:
                break
            # Re-priming on reconnect keeps a backlog re-send from being
            # announced as new.
            self._primed = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RECONNECT_MAX_SECONDS)

    async def _subscribe_forever(self) -> None:
        key = _env("UNRAID_API_KEY")
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(
            _ws_url(),
            protocols=(_WS_PROTOCOL,),
            headers={"x-api-key": key},
            ssl=_ssl_context(),
            heartbeat=30,
        )
        await self._ws.send_json({"type": "connection_init", "payload": {"x-api-key": key}})

        acked = False
        deadline = time.monotonic() + _ACK_TIMEOUT_SECONDS
        async for msg in self._ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                raise RuntimeError(f"unexpected websocket frame: {msg.type}")
            data = json.loads(msg.data)
            kind = data.get("type")
            if kind == "connection_ack":
                acked = True
                await self._ws.send_json(
                    {"id": "unraid-alerts", "type": "subscribe",
                     "payload": {"query": _SUBSCRIPTION}}
                )
                logger.info("[Unraid] subscribed to notificationsWarningsAndAlerts")
            elif kind == "next":
                await self._handle_payload(data.get("payload") or {})
            elif kind in ("error", "connection_error"):
                raise RuntimeError(f"subscription error: {json.dumps(data)[:300]}")
            elif kind == "complete":
                raise RuntimeError("server completed the subscription")
            if not acked and time.monotonic() > deadline:
                raise RuntimeError("no connection_ack within timeout")

    async def _handle_payload(self, payload: Dict[str, Any]) -> None:
        items = (payload.get("data") or {}).get("notificationsWarningsAndAlerts")
        if items is None:
            return
        if isinstance(items, dict):
            items = [items]

        settings = _settings()
        if not settings["alerts_enabled"]:
            return

        # Seed on the first payload: it is the existing backlog, not news.
        if not self._primed:
            for n in items:
                self._remember(str(n.get("id") or ""))
            self._primed = True
            logger.info("[Unraid] primed with %d existing alert(s)", len(items))
            return

        threshold = _IMPORTANCE_RANK[settings["min_importance"]]
        for n in items:
            nid = str(n.get("id") or "")
            if not nid or nid in self._seen:
                continue
            importance = str(n.get("importance") or "INFO").upper()
            if _IMPORTANCE_RANK.get(importance, 0) < threshold:
                self._remember(nid)
                continue
            if not self._rate_ok(n, settings):
                self._remember(nid)
                continue
            self._remember(nid)
            await self._emit(n, importance)

    def _remember(self, nid: str) -> None:
        if not nid or nid in self._seen:
            return
        self._seen.add(nid)
        self._seen_order.append(nid)
        while len(self._seen_order) > _SEEN_MAX:
            self._seen.discard(self._seen_order.pop(0))

    def _rate_ok(self, notification: Dict[str, Any], settings: Dict[str, Any]) -> bool:
        """Per-subject cooldown plus a global hourly ceiling.

        Cooldown keys on subject rather than id because a flapping condition
        raises a fresh id each time, which an id-based cooldown would never
        catch.
        """
        now = time.time()
        self._emitted_times = [t for t in self._emitted_times if now - t < 3600]
        if settings["max_per_hour"] and len(self._emitted_times) >= settings["max_per_hour"]:
            logger.warning("[Unraid] hourly alert ceiling reached, suppressing")
            return False
        subject = str(notification.get("subject") or notification.get("title") or "")
        last = self._last_emit.get(subject, 0.0)
        if settings["cooldown_seconds"] and now - last < settings["cooldown_seconds"]:
            return False
        self._last_emit[subject] = now
        self._emitted_times.append(now)
        return True

    async def _emit(self, n: Dict[str, Any], importance: str) -> None:
        title = n.get("title") or "Unraid"
        subject = n.get("subject") or ""
        description = (n.get("description") or "").strip()
        link = n.get("link") or ""
        text = f"[Unraid {importance}] {title}: {subject}"
        if description:
            text += f"\n{description}"
        if link:
            text += f"\n{link}"

        source = self.build_source(
            chat_id="unraid_alerts",
            chat_name="Unraid Alerts",
            chat_type="channel",
            user_id="unraid",
            user_name="Unraid",
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=f"unraid_{n.get('id')}",
            timestamp=datetime.now(),
        )
        logger.info("[Unraid] forwarding %s alert: %s", importance, subject[:80])
        await self.handle_message(event)

    # -- outbound ----------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Deliver a message as an Unraid notification.

        Requires NOTIFICATIONS:CREATE_ANY on the API key. A read-only or
        update-only key returns FORBIDDEN, which is reported as-is rather than
        being retried, since no amount of retrying will grant a permission.
        """
        if not AIOHTTP_AVAILABLE:
            return SendResult(success=False, error="aiohttp is not installed")
        body = (content or "").strip()
        if not body:
            return SendResult(success=False, error="empty message")
        subject, _, rest = body.partition("\n")
        payload = {
            "query": "mutation($input: NotificationData!) { createNotification(input: $input) { id } }",
            "variables": {
                "input": {
                    "title": "Hermes",
                    "subject": subject[:200] or "Hermes",
                    "description": (rest.strip() or subject)[: self.MAX_MESSAGE_LENGTH],
                    "importance": (metadata or {}).get("importance", "INFO"),
                }
            },
        }
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    _env("UNRAID_API_URL"),
                    json=payload,
                    headers={"x-api-key": _env("UNRAID_API_KEY"),
                             "Content-Type": "application/json"},
                    ssl=_ssl_context(),
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    data = await resp.json()
        except Exception as e:  # noqa: BLE001
            return SendResult(success=False, error=f"{type(e).__name__}: {e}")
        if "errors" in data:
            message = (data["errors"] or [{}])[0].get("message", "unknown error")
            if "forbidden" in str(message).lower():
                message = (
                    "createNotification requires NOTIFICATIONS:CREATE_ANY on the Unraid API key"
                )
            return SendResult(success=False, error=message)
        nid = ((data.get("data") or {}).get("createNotification") or {}).get("id")
        return SendResult(success=True, message_id=str(nid) if nid else None)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {
            "id": chat_id or "unraid_alerts",
            "name": "Unraid Alerts",
            "type": "channel",
        }


def _build_adapter(config):
    return UnraidAdapter(config)


def register_platform(ctx) -> None:
    """Register the platform adapter. Called from the plugin's register()."""
    ctx.register_platform(
        name="unraid",
        label="Unraid",
        adapter_factory=_build_adapter,
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=_is_connected,
        required_env=["UNRAID_API_URL", "UNRAID_API_KEY"],
        install_hint="pip install aiohttp",
        max_message_length=UnraidAdapter.MAX_MESSAGE_LENGTH,
        emoji="🗄️",
        allow_update_command=True,
    )
