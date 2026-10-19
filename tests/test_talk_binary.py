"""Proves send_talk_binary builds its frame on the installed reolink-aio.

reolink-aio 0.21.17 removed baichuan.xmls.XML_HEADER and every talk block
raised AttributeError before reaching the camera (issue #4). This runs the
real function against the real library, so it fails on any version that
drops something send_talk_binary still reaches for.

talk.py imports Home Assistant at module level, so stub just enough of it to
load the module without a Home Assistant install.
"""
import asyncio
import importlib.util
import pathlib
import sys
import types
import unittest
import xml.etree.ElementTree as ET

for name in ("homeassistant", "homeassistant.core", "homeassistant.helpers",
             "homeassistant.helpers.aiohttp_client", "homeassistant.helpers.network"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["homeassistant.core"].HomeAssistant = object
sys.modules["homeassistant.helpers.aiohttp_client"].async_get_clientsession = None
sys.modules["homeassistant.helpers.network"].get_url = None

_path = pathlib.Path(__file__).parent.parent / "custom_components/reolink_talk/talk.py"
_spec = importlib.util.spec_from_file_location("talk_binary_under_test", _path)
talk = importlib.util.module_from_spec(_spec)
sys.modules["talk_binary_under_test"] = talk
_spec.loader.exec_module(talk)

HAVE_REOLINK_AIO = importlib.util.find_spec("reolink_aio") is not None


class FakeBaichuan:
    """Just the surface send_talk_binary touches; AES is identity so the frame is readable."""

    _logged_in = True
    _mess_id = 0

    def __init__(self):
        self.written = []
        self._login_mutex = asyncio.Lock()
        self._connection = types.SimpleNamespace(
            _transport=types.SimpleNamespace(write=self.written.append)
        )

    def _aes_encrypt(self, data: bytes) -> bytes:
        return data

    async def _connect_if_needed(self):
        pass


@unittest.skipUnless(HAVE_REOLINK_AIO, "needs reolink-aio installed (pip install reolink-aio)")
class SendTalkBinaryTest(unittest.TestCase):
    def test_frame_carries_extension_xml_then_payload(self):
        bc = FakeBaichuan()
        payload = b"\x00\x01\x02\x03"
        asyncio.run(talk.send_talk_binary(bc, channel=2, binary_payload=payload))

        self.assertEqual(len(bc.written), 1)
        packet = bc.written[0]
        header_len = 24
        payload_offset = int.from_bytes(packet[20:24], "little")
        ext = packet[header_len:header_len + payload_offset].decode("utf-8")

        self.assertTrue(ext.startswith('<?xml version="1.0" encoding="UTF-8" ?>\n'))
        root = ET.fromstring(ext)
        self.assertEqual(root.findtext("binaryData"), "1")
        self.assertEqual(root.findtext("channelId"), "2")
        self.assertEqual(packet[header_len + payload_offset:], payload)


if __name__ == "__main__":
    unittest.main()
