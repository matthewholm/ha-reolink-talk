"""Proves a live-talk session always ends with a talk stop (cmd 11) sent to the camera.

Covers the two cases where the browser never sends a close frame: a client that
stops answering pings, and a session that hits the maximum duration. Stubs just
enough of Home Assistant and reolink_aio to run the real view on an aiohttp
test server.
"""
import asyncio
import enum
import importlib.util
import pathlib
import sys
import types
import unittest

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer


def _stub(name, **attrs):
    mod = sys.modules.setdefault(name, types.ModuleType(name))
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


class _ApiError(Exception):
    rspCode = None


class _EncType(enum.Enum):
    AES = "aes"
    BC = "bc"


_stub("homeassistant")
_stub("homeassistant.components")
_stub("homeassistant.components.websocket_api",
      websocket_command=lambda schema: (lambda f: f),
      async_register_command=lambda hass, f: None)
_stub("homeassistant.components.http", HomeAssistantView=object)
_stub("homeassistant.config_entries", ConfigEntry=object)
_stub("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_stub("homeassistant.util", slugify=lambda s: s.lower().replace(" ", "_"))
_stub("homeassistant.helpers")
_stub("homeassistant.helpers.aiohttp_client", async_get_clientsession=None)
_stub("homeassistant.helpers.network", get_url=None)
_stub("reolink_aio")
_stub("reolink_aio.baichuan")
_stub("reolink_aio.baichuan.util", EncType=_EncType)
_stub("reolink_aio.exceptions", ApiError=_ApiError)
sys.modules["reolink_aio.baichuan"].util = sys.modules["reolink_aio.baichuan.util"]

_pkg_dir = pathlib.Path(__file__).parent.parent / "custom_components/reolink_talk"
_pkg = types.ModuleType("reolink_talk_under_test")
_pkg.__path__ = [str(_pkg_dir)]
sys.modules["reolink_talk_under_test"] = _pkg
_spec = importlib.util.spec_from_file_location(
    "reolink_talk_under_test.talk_live_view", _pkg_dir / "talk_live_view.py"
)
view_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = view_mod
_spec.loader.exec_module(view_mod)

ABILITY_XML = (
    "<body><TalkAbility><duplexList><duplex>FDX</duplex></duplexList>"
    "<audioStreamModeList><audioStreamMode>mixAudioStream</audioStreamMode></audioStreamModeList>"
    "<audioConfigList><audioConfig><audioType>adpcm</audioType><sampleRate>16000</sampleRate>"
    "<lengthPerEncoder>1024</lengthPerEncoder></audioConfig></audioConfigList>"
    "</TalkAbility></body>"
)


class FakeBaichuan:
    def __init__(self):
        self.cmds = []

    async def send(self, cmd_id, channel=None, body="", enc_type=None, **_kw):
        self.cmds.append(cmd_id)
        return ABILITY_XML if cmd_id == 10 else ""


class LiveSessionEndTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bc = FakeBaichuan()
        entry = types.SimpleNamespace(
            runtime_data=types.SimpleNamespace(
                host=types.SimpleNamespace(api=types.SimpleNamespace(baichuan=self.bc))
            )
        )
        self._saved = {
            name: getattr(view_mod, name, None)
            for name in ("_consume_token", "_resolve_camera", "send_talk_binary",
                         "LIVE_TALK_HEARTBEAT_SECONDS", "LIVE_TALK_MAX_SECONDS")
        }
        view_mod._consume_token = lambda hass, token, camera: True
        view_mod._resolve_camera = lambda hass, camera: (entry, 0)

        async def _no_send(*_a, **_kw):
            return None

        view_mod.send_talk_binary = _no_send

        app = web.Application()
        app["hass"] = object()
        app.router.add_get("/live", view_mod.ReolinkTalkLiveWebSocketView().get)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for name, value in self._saved.items():
            setattr(view_mod, name, value)

    async def _wait_for_stop(self, seconds):
        for _ in range(int(seconds / 0.05)):
            if 11 in self.bc.cmds:
                return True
            await asyncio.sleep(0.05)
        return False

    async def test_client_close_sends_stop(self):
        ws = await self.client.ws_connect("/live?camera=front_door&token=t")
        self.assertEqual((await ws.receive_json())["status"], "ready")
        await ws.send_bytes(b"\x00\x00" * 160)
        await ws.close()
        self.assertTrue(await self._wait_for_stop(2))

    async def test_max_duration_closes_and_sends_stop(self):
        view_mod.LIVE_TALK_MAX_SECONDS = 0.5
        ws = await self.client.ws_connect("/live?camera=front_door&token=t")
        self.assertEqual((await ws.receive_json())["status"], "ready")
        msg = await ws.receive(timeout=3)
        self.assertEqual(msg.type, WSMsgType.CLOSE)
        self.assertEqual(msg.data, 4004)
        self.assertTrue(await self._wait_for_stop(2))

    async def test_silent_client_is_dropped_and_stop_sent(self):
        view_mod.LIVE_TALK_HEARTBEAT_SECONDS = 0.4
        # autoping=False: the client never answers pings, like a phone that
        # lost its network without closing the socket.
        ws = await self.client.ws_connect("/live?camera=front_door&token=t", autoping=False)
        self.assertEqual((await ws.receive_json())["status"], "ready")
        self.assertTrue(await self._wait_for_stop(3))



class ClientErrorTest(unittest.TestCase):
    def test_client_error_lands_in_the_diagnostic_sensor(self):
        states, results = {}, []
        hass = types.SimpleNamespace(states=types.SimpleNamespace(
            async_set=lambda eid, state, attrs: states.__setitem__(eid, (state, attrs))))
        conn = types.SimpleNamespace(send_result=lambda mid, *a: results.append(mid))
        view_mod.ws_client_error(hass, conn, {"id": 7, "camera": "front_door",
                                              "message": "mic error - NotAllowedError: denied",
                                              "context": "secure=true"})
        state, attrs = states[view_mod.CLIENT_ERROR_ENTITY]
        self.assertEqual(state, "mic error - NotAllowedError: denied")
        self.assertEqual((attrs["camera"], attrs["context"]), ("front_door", "secure=true"))
        self.assertEqual(results, [7])

if __name__ == "__main__":
    unittest.main()
