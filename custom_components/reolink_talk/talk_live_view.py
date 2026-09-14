"""Live (streaming) push-to-talk HTTP/WebSocket views for reolink_talk.

Reuses the already-verified building blocks from talk.py (TalkAbility parsing,
TalkConfig XML variants, the DVI-4 ADPCM encoder, BcMedia framing, and the
low-level send_talk_binary sender) but instead of encoding one pre-built file
and sending it once, it accepts a continuous stream of raw PCM16LE mono audio
over a WebSocket (from a browser microphone) and forwards it to the camera
block-by-block in near real time.

Runs inside Home Assistant's own HTTP server -- no separate process, no
separate reverse-proxy host needed.

Authentication
--------------
The audio WebSocket itself cannot use Home Assistant's normal bearer-token
auth: the browser's WebSocket API cannot set request headers, and the mic
capture path needs to open the socket directly. Instead, a short-lived
single-use token is issued over Home Assistant's *authenticated* WebSocket API
(`reolink_talk/get_token`) and must be presented as a query parameter when
opening the audio socket. An unauthenticated caller has no way to obtain one.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time

import voluptuous as vol
from aiohttp import WSMsgType, web

from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import slugify

from .const import DOMAIN
from .talk import (
    build_talk_config_variants,
    ima_adpcm_encode_dvi_blocks,
    parse_talk_ability,
    send_talk_binary,
    talk_binary_payload,
)

_LOGGER = logging.getLogger(__name__)

# How long an issued live-talk token stays valid. Long enough to cover a slow
# mic permission prompt, short enough that a leaked token is worthless.
TOKEN_TTL_SECONDS = 30

TOKEN_STORE_KEY = "live_talk_tokens"


def _token_store(hass: HomeAssistant) -> dict[str, tuple[str, float]]:
    """Return the {token: (camera_slug, expires_at)} store, creating it if needed."""
    return hass.data.setdefault(DOMAIN, {}).setdefault(TOKEN_STORE_KEY, {})


def _purge_expired(store: dict[str, tuple[str, float]]) -> None:
    now = time.monotonic()
    for token in [t for t, (_cam, exp) in store.items() if exp < now]:
        store.pop(token, None)


def _issue_token(hass: HomeAssistant, camera_slug: str) -> str:
    """Mint a single-use token bound to one camera."""
    store = _token_store(hass)
    _purge_expired(store)
    token = secrets.token_urlsafe(32)
    store[token] = (camera_slug, time.monotonic() + TOKEN_TTL_SECONDS)
    return token


def _consume_token(hass: HomeAssistant, token: str | None, camera_slug: str | None) -> bool:
    """Validate and burn a token. Returns True only for a live, matching token."""
    if not token or not camera_slug:
        return False
    store = _token_store(hass)
    _purge_expired(store)
    entry = store.get(token)
    if entry is None:
        return False
    issued_for, expires_at = entry
    if expires_at < time.monotonic():
        return False
    # Bound to the camera it was issued for, so a token for a camera you can
    # see can't be replayed against a different one.
    return secrets.compare_digest(issued_for, camera_slug)


def _iter_camera_slugs(hass: HomeAssistant):
    """Yield (slug, channel, config_entry) for every channel on every configured
    Reolink hub.

    The slug is derived from the camera name the hub itself reports -- the same
    name used to build this integration's `media_player` entities -- so there is
    nothing to hand-configure or keep in sync after a HACS update.
    """
    reolink_entry_ids = hass.data.get(DOMAIN, {}).get("reolink_entry_ids")
    if not reolink_entry_ids:
        reolink_entry_ids = [e.entry_id for e in hass.config_entries.async_entries("reolink")]

    for entry_id in reolink_entry_ids:
        entry: ConfigEntry | None = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            continue
        runtime_data = getattr(entry, "runtime_data", None)
        host = getattr(runtime_data, "host", None)
        api = getattr(host, "api", None)
        if api is None:
            # Reolink entry exists but isn't fully set up (e.g. camera offline
            # at HA startup); skip it rather than failing the whole lookup.
            continue
        for ch in api.channels:
            try:
                name = api.camera_name(ch) or f"channel_{ch}"
            except Exception:
                name = f"channel_{ch}"
            yield slugify(name), ch, entry


def _resolve_camera(hass: HomeAssistant, camera_query: str) -> tuple[ConfigEntry, int] | None:
    for slug, ch, entry in _iter_camera_slugs(hass):
        if slug == camera_query:
            return entry, ch
    return None


@websocket_api.websocket_command(
    {
        vol.Required("type"): "reolink_talk/get_token",
        vol.Required("camera"): str,
    }
)
@callback
def ws_get_token(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Issue a short-lived token for opening the live-talk audio socket.

    This runs on Home Assistant's authenticated WebSocket API, so only a
    logged-in user can reach it.
    """
    camera = msg["camera"]
    if _resolve_camera(hass, camera) is None:
        available = sorted({slug for slug, _ch, _e in _iter_camera_slugs(hass)})
        hint = ", ".join(available) if available else "none discovered yet"
        connection.send_error(
            msg["id"],
            "unknown_camera",
            f"unknown camera {camera!r}. Available: {hint}",
        )
        return
    connection.send_result(
        msg["id"],
        {"token": _issue_token(hass, camera), "ttl": TOKEN_TTL_SECONDS},
    )


class ReolinkTalkLiveWebSocketView(HomeAssistantView):
    """Accepts a live mic PCM stream and forwards it to the camera as talk audio."""

    url = "/api/reolink_talk/live_ws"
    name = "api:reolink_talk:live_ws"
    # The browser WebSocket API cannot send an Authorization header, so this
    # view authenticates via the single-use token issued over HA's own
    # authenticated WebSocket API instead. See _consume_token().
    requires_auth = False

    async def get(self, request: web.Request):
        hass: HomeAssistant = request.app["hass"]

        camera = request.query.get("camera")
        token = request.query.get("token")

        # Authenticate BEFORE upgrading the connection or touching camera
        # lookups, so an unauthenticated caller learns nothing at all -- not
        # even which cameras exist.
        if not _consume_token(hass, token, camera):
            _LOGGER.warning(
                "Live talk: rejected unauthenticated connection from %s (camera=%r)",
                request.remote,
                camera,
            )
            return web.Response(status=401, text="invalid or expired talk token")

        ws = web.WebSocketResponse(max_msg_size=0)
        await ws.prepare(request)

        resolved = _resolve_camera(hass, camera)
        if resolved is None:
            available = sorted({slug for slug, _ch, _e in _iter_camera_slugs(hass)})
            hint = ", ".join(available) if available else "none discovered yet (is the Reolink integration loaded?)"
            _LOGGER.error("Live talk: unknown/missing camera=%r. Available: %s", camera, hint)
            await ws.close(
                code=4000,
                message=f"unknown camera {camera!r}. Available: {hint}".encode(),
            )
            return ws
        entry, channel = resolved

        _LOGGER.info("Live talk connected: camera=%s channel=%s", camera, channel)

        from reolink_aio.baichuan import util as bc_util
        from reolink_aio.exceptions import ApiError

        # Reuse the Reolink core integration's already-connected Host/Baichuan
        # session instead of opening a second, parallel connection to the Home
        # Hub. The hub does not reliably handle multiple simultaneous Baichuan
        # sessions -- opening a second one was causing intermittent UDP
        # timeouts and stuck (421) talk sessions.
        reolink_host = entry.runtime_data.host
        api_host = reolink_host.api
        bc = api_host.baichuan
        enc_used = None
        try:
            ability_xml = await bc.send(cmd_id=10, channel=channel)
            ability = parse_talk_ability(ability_xml)
            if ability.audio_type.lower() != "adpcm":
                _LOGGER.error("Camera ch=%s does not support ADPCM talk (got %s)", channel, ability.audio_type)
                await ws.close(code=4002, message=b"camera does not support adpcm talk")
                return ws

            full_block = (int(ability.length_per_encoder) // 2) + 4
            payload_bytes = full_block - 4
            samples_per_block = payload_bytes * 2
            bytes_per_block_pcm = samples_per_block * 2  # s16le -> 2 bytes/sample

            _LOGGER.info(
                "TalkAbility ch=%s sampleRate=%s lengthPerEncoder=%s full_block=%s pcm_bytes_per_block=%s",
                channel, ability.sample_rate, ability.length_per_encoder, full_block, bytes_per_block_pcm,
            )

            # Start talk session -- same TalkConfig send/fallback dance as
            # talk_playback() in talk.py, kept local here to avoid touching
            # the already-verified file-playback code path.
            last_err = None
            for cfg_xml in build_talk_config_variants(channel, ability):
                for enc in (bc_util.EncType.AES, bc_util.EncType.BC):
                    try:
                        await bc.send(cmd_id=201, channel=channel, body=cfg_xml, enc_type=enc)
                        enc_used = enc
                        last_err = None
                        break
                    except ApiError as err:
                        last_err = err
                        rsp = getattr(err, "rspCode", None)
                        if rsp in (400, 421, 422):
                            try:
                                await bc.send(cmd_id=11, channel=channel, enc_type=enc)
                                await asyncio.sleep(0.1)
                                await bc.send(cmd_id=201, channel=channel, body=cfg_xml, enc_type=enc)
                                enc_used = enc
                                last_err = None
                                break
                            except Exception:
                                pass
                        continue
                if enc_used is not None:
                    break

            if enc_used is None:
                _LOGGER.error("Live talk: TalkConfig rejected for ch=%s (%s)", channel, last_err)
                await ws.close(code=4003, message=b"camera rejected talk config")
                return ws

            _LOGGER.info("Live talk session started: camera=%s channel=%s enc=%s", camera, channel, enc_used.value)
            await ws.send_json({"status": "ready", "sampleRate": ability.sample_rate})

            pcm_buffer = bytearray()
            stream_block_number = 0
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    pcm_buffer += msg.data
                    while len(pcm_buffer) >= bytes_per_block_pcm:
                        chunk = bytes(pcm_buffer[:bytes_per_block_pcm])
                        del pcm_buffer[:bytes_per_block_pcm]
                        adpcm_block = ima_adpcm_encode_dvi_blocks(chunk, full_block_size=full_block)
                        if not adpcm_block:
                            continue
                        for payload, _n in talk_binary_payload(
                            adpcm_block,
                            full_block,
                            blocks_per_payload=1,
                            block_number_start=stream_block_number,
                        ):
                            await send_talk_binary(bc, channel, payload, enc_type=enc_used)
                            stream_block_number += 1
                elif msg.type == WSMsgType.ERROR:
                    _LOGGER.warning("Live talk WS error: %s", ws.exception())
                    break
                # TEXT control messages are ignored; the browser closing the
                # socket is what ends the loop.

            _LOGGER.info("Live talk disconnected: camera=%s channel=%s", camera, channel)
        except Exception:
            _LOGGER.exception("Live talk relay error (camera=%s)", camera)
        finally:
            if enc_used is not None:
                try:
                    await bc.send(cmd_id=11, channel=channel, enc_type=enc_used)
                except Exception as e:
                    _LOGGER.warning("cmd11 stop failed (ignored): %s", e)
            # Do NOT logout/close bc here -- this connection is owned by the
            # core Reolink integration, we only borrowed it.

        return ws


def async_register_views(hass: HomeAssistant) -> None:
    """Register the live-talk views and token command exactly once."""
    key = "reolink_talk_live_views_registered"
    if hass.data.get(key):
        return
    hass.data[key] = True
    hass.http.register_view(ReolinkTalkLiveWebSocketView())
    websocket_api.async_register_command(hass, ws_get_token)
    _LOGGER.info("Registered reolink_talk live-talk WebSocket view at /api/reolink_talk/live_ws")
