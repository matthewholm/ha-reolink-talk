"""Proves LiveAdpcmEncoder keeps codec state across blocks.

talk.py imports Home Assistant at module level, so stub just enough of it to
load the pure encoder code without a Home Assistant install.
"""
import importlib.util
import math
import pathlib
import struct
import sys
import types
import unittest

for name in ("homeassistant", "homeassistant.core", "homeassistant.helpers",
             "homeassistant.helpers.aiohttp_client", "homeassistant.helpers.network"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["homeassistant.core"].HomeAssistant = object
sys.modules["homeassistant.helpers.aiohttp_client"].async_get_clientsession = None
sys.modules["homeassistant.helpers.network"].get_url = None

_path = pathlib.Path(__file__).parent.parent / "custom_components/reolink_talk/talk.py"
_spec = importlib.util.spec_from_file_location("talk_under_test", _path)
talk = importlib.util.module_from_spec(_spec)
sys.modules["talk_under_test"] = talk
_spec.loader.exec_module(talk)

BLOCK = 260  # a typical lengthPerEncoder-derived block: 4 header + 256 payload


def decode(blocks):
    """Reference DVI-4 decoder that trusts each block header, like the camera."""
    out = []
    for b in blocks:
        predictor, idx, _ = struct.unpack("<hBB", b[:4])
        for byte in b[4:]:
            for nib in (byte & 0xF, byte >> 4):
                step = talk._IMA_STEP_TABLE[idx]
                vpdiff = step >> 3
                if nib & 4: vpdiff += step
                if nib & 2: vpdiff += step >> 1
                if nib & 1: vpdiff += step >> 2
                predictor += -vpdiff if nib & 8 else vpdiff
                predictor = max(-32768, min(32767, predictor))
                idx = max(0, min(88, idx + talk._IMA_INDEX_TABLE[nib]))
                out.append(predictor)
    return out


def sine(n, hz=440, rate=16000, amp=12000):
    return [int(amp * math.sin(2 * math.pi * hz * i / rate)) for i in range(n)]


def snr_db(ref, got):
    sig = sum(x * x for x in ref)
    err = sum((a - b) ** 2 for a, b in zip(ref, got)) or 1
    return 10 * math.log10(sig / err)


class LiveEncoderTest(unittest.TestCase):
    def encode_stream(self, pcm, chunk_samples):
        enc = talk.LiveAdpcmEncoder(BLOCK)
        blocks = []
        for i in range(0, len(pcm), chunk_samples):
            blocks += enc.feed(struct.pack(f"<{len(pcm[i:i+chunk_samples])}h", *pcm[i:i+chunk_samples]))
        return blocks

    def test_long_hold_stays_accurate(self):
        pcm = sine(16000 * 20)  # 20 s
        blocks = self.encode_stream(pcm, 1486)  # odd chunking, like 44.1 kHz capture
        got = decode(blocks)
        # Compare the last 5 s: a codec that drifted would be far off by now.
        tail = slice(len(got) - 16000 * 5, len(got))
        offset = 1  # first sample seeds the predictor and is not emitted
        self.assertGreater(snr_db(pcm[offset:][tail], got[tail]), 15)

    def test_chunking_does_not_change_output(self):
        pcm = sine(16000 * 2)
        a = self.encode_stream(pcm, 4096)
        b = self.encode_stream(pcm, 333)
        self.assertEqual(a, b)

    def test_step_index_carried_between_blocks(self):
        blocks = self.encode_stream(sine(16000), 4096)
        # With reset-per-block encoding every header step index would be 0.
        self.assertTrue(any(b[2] != 0 for b in blocks[1:]))

    def test_old_per_block_call_resets_state(self):
        """Documents the defect: one call per block always restarts at index 0."""
        pcm = sine(600)
        chunk = struct.pack("<261h", *pcm[:261])
        second = struct.pack("<261h", *pcm[261:522])
        talk.ima_adpcm_encode_dvi_blocks(chunk, full_block_size=BLOCK)
        blk = talk.ima_adpcm_encode_dvi_blocks(second, full_block_size=BLOCK)
        self.assertEqual(blk[2], 0)


if __name__ == "__main__":
    unittest.main()
