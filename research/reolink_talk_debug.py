#!/usr/bin/env python3
"""
Reolink two-way audio (talk) debug harness.

Goals:
- Use the same credentials as the official Home Assistant Reolink integration
  by reading .storage/core.config_entries (domain=reolink).
- Fetch TalkAbility (cmd 10), try TalkConfig (cmd 201) with AES and BC encryption.
- Send ADPCM talk frames (cmd 202) and stop (cmd 11).
- Produce very explicit logs so we can see where the camera rejects the flow.

Run:
  python3 scripts/reolink_talk_debug.py --list
  python3 scripts/reolink_talk_debug.py --title Deurbel --file media/doorbell.mp3
  python3 scripts/reolink_talk_debug.py --title Deurbel --sine 1000 --duration 2

Notes:
- This script creates/uses a local venv under ./.venv-reolink-talk-debug
  and installs reolink-aio + aiohttp + pycryptodome if needed.
- The script does NOT depend on Home Assistant Python packages.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Final


LOG = logging.getLogger("reolink_talk_debug")

BC_MESSAGE_CLASS_1464: Final[bytes] = bytes.fromhex("00001464")  # status_code=0, class=1464
# reolink-aio 0.21.17 removed baichuan.xmls.XML_HEADER; same bytes, kept locally.
BC_XML_HEADER: Final[str] = '<?xml version="1.0" encoding="UTF-8" ?>\n'
ROOT: Final[Path] = Path(__file__).resolve().parents[1]


def _ensure_venv() -> None:
    """Re-exec inside a local venv with required dependencies installed."""
    venv = ROOT / ".venv-reolink-talk-debug"
    py = venv / "bin" / "python"
    pip = venv / "bin" / "pip"

    if os.environ.get("REOLINK_TALK_DEBUG_VENV") == "1":
        return

    if not py.exists():
        LOG.info("Creating venv at %s", venv)
        subprocess.check_call([sys.executable, "-m", "venv", str(venv)])
        # Ensure pip exists even on minimal venv setups.
        try:
            subprocess.check_call([str(py), "-m", "ensurepip", "--upgrade"])
        except Exception:
            pass

    if not pip.exists():
        # Some Python installs don't create a pip wrapper in venv/bin, but
        # `python -m pip` still works once ensurepip ran.
        pip = None

    # Install deps if missing.
    try:
        subprocess.check_call([str(py), "-c", "import reolink_aio, aiohttp, Crypto"])
    except Exception:
        LOG.info("Installing dependencies into %s", venv)
        if pip is not None:
            subprocess.check_call([str(pip), "install", "--upgrade", "pip"])
            subprocess.check_call([str(pip), "install", "reolink-aio", "aiohttp", "pycryptodome"])
        else:
            subprocess.check_call([str(py), "-m", "pip", "install", "--upgrade", "pip"])
            subprocess.check_call([str(py), "-m", "pip", "install", "reolink-aio", "aiohttp", "pycryptodome"])

    env = os.environ.copy()
    env["REOLINK_TALK_DEBUG_VENV"] = "1"
    LOG.info("Re-exec inside venv python: %s", py)
    os.execve(str(py), [str(py), *sys.argv], env)


@dataclass(frozen=True)
class TalkAbility:
    duplex: str
    audio_stream_mode: str
    audio_type: str
    priority: int | None
    sample_rate: int
    sample_precision: int
    length_per_encoder: int
    sound_track: str


def _first_text(root: ET.Element, path: str) -> str | None:
    el = root.find(path)
    if el is None or el.text is None:
        return None
    return el.text.strip()

def _all_texts(root: ET.Element, path: str) -> list[str]:
    out: list[str] = []
    for el in root.findall(path):
        if el is None or el.text is None:
            continue
        t = el.text.strip()
        if t:
            out.append(t)
    return out


def parse_talk_ability(xml: str) -> TalkAbility:
    root = ET.fromstring(xml)
    ta = root.find(".//TalkAbility")
    if ta is None:
        raise ValueError("TalkAbility not found in response")

    duplex_list = _all_texts(ta, ".//duplexList/duplex")
    stream_mode_list = _all_texts(ta, ".//audioStreamModeList/audioStreamMode")

    duplex = _first_text(ta, ".//duplex") or ""
    if "FDX" in duplex_list:
        duplex = "FDX"
    if not duplex:
        duplex = duplex_list[0] if duplex_list else "FDX"

    audio_stream_mode = _first_text(ta, ".//audioStreamMode") or ""
    if "mixAudioStream" in stream_mode_list:
        audio_stream_mode = "mixAudioStream"
    if not audio_stream_mode:
        audio_stream_mode = stream_mode_list[0] if stream_mode_list else "followVideoStream"

    ac = ta.find(".//audioConfig")
    if ac is None:
        raise ValueError("audioConfig not found in TalkAbility")

    audio_type = _first_text(ac, ".//audioType") or "adpcm"
    prio_txt = _first_text(ac, ".//priority")
    priority = int(prio_txt) if prio_txt and prio_txt.isdigit() else None
    sample_rate = int(_first_text(ac, ".//sampleRate") or "16000")
    sample_precision = int(_first_text(ac, ".//samplePrecision") or "16")
    length_per_encoder = int(_first_text(ac, ".//lengthPerEncoder") or "1024")
    sound_track = _first_text(ac, ".//soundTrack") or "mono"

    return TalkAbility(
        duplex=duplex,
        audio_stream_mode=audio_stream_mode,
        audio_type=audio_type,
        priority=priority,
        sample_rate=sample_rate,
        sample_precision=sample_precision,
        length_per_encoder=length_per_encoder,
        sound_track=sound_track,
    )


def build_talk_config_xml(channel: int, ability: TalkAbility) -> str:
    prio = f"<priority>{ability.priority}</priority>\n" if ability.priority is not None else ""
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>\n'
        "<body>\n"
        '<TalkConfig version="1.1">\n'
        f"<channelId>{channel}</channelId>\n"
        f"<duplex>{ability.duplex}</duplex>\n"
        f"<audioStreamMode>{ability.audio_stream_mode}</audioStreamMode>\n"
        "<audioConfig>\n"
        + prio
        + f"<audioType>{ability.audio_type}</audioType>\n"
        + f"<sampleRate>{ability.sample_rate}</sampleRate>\n"
        + f"<samplePrecision>{ability.sample_precision}</samplePrecision>\n"
        + f"<lengthPerEncoder>{ability.length_per_encoder}</lengthPerEncoder>\n"
        + f"<soundTrack>{ability.sound_track}</soundTrack>\n"
        + "</audioConfig>\n"
        + "</TalkConfig>\n"
        + "</body>\n"
    )


def build_talk_config_variants(channel: int, ability: TalkAbility) -> list[str]:
    full = build_talk_config_xml(channel, ability)
    variants: list[str] = [full]

    if full.lstrip().startswith("<?xml"):
        try:
            _, rest = full.split("\n", 1)
            variants.append(rest)
        except ValueError:
            pass

    start = full.find("<TalkConfig")
    end = full.rfind("</TalkConfig>")
    if start != -1 and end != -1:
        tc = full[start : end + len("</TalkConfig>")] + "\n"
        if tc not in variants:
            variants.append(tc)

    return variants


def _riff_chunks(wav: bytes):
    if len(wav) < 12 or wav[0:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise ValueError("Not a RIFF/WAVE file")
    off = 12
    while off + 8 <= len(wav):
        cid = wav[off : off + 4]
        size = int.from_bytes(wav[off + 4 : off + 8], "little")
        data_off = off + 8
        data_end = data_off + size
        truncated = data_end > len(wav)
        if truncated:
            data_end = len(wav)
        yield cid, wav[data_off:data_end]
        if truncated:
            break
        off = data_end + (size % 2)


def extract_wav_fmt_and_data(wav: bytes) -> tuple[dict, bytes]:
    fmt: dict = {}
    data: bytes | None = None
    for cid, payload in _riff_chunks(wav):
        if cid == b"fmt ":
            if len(payload) < 16:
                raise ValueError("Invalid fmt chunk")
            fmt["audio_format"] = int.from_bytes(payload[0:2], "little")
            fmt["channels"] = int.from_bytes(payload[2:4], "little")
            fmt["sample_rate"] = int.from_bytes(payload[4:8], "little")
            fmt["byte_rate"] = int.from_bytes(payload[8:12], "little")
            fmt["block_align"] = int.from_bytes(payload[12:14], "little")
            fmt["bits_per_sample"] = int.from_bytes(payload[14:16], "little")
        elif cid == b"data":
            data = payload
    if not fmt or data is None:
        raise ValueError("WAV missing fmt or data chunk")
    return fmt, data


def bcmedia_adpcm_packet(block: bytes) -> bytes:
    if len(block) < 5:
        raise ValueError("ADPCM block too small")
    payload_len = len(block) + 4
    # Default interpretation seems to be "samples per block", but some firmwares
    # might interpret this differently. We allow overriding via CLI.
    samples_per_block = (len(block) - 4) * 2 + 1
    header = struct.pack(
        "<IHHHH",
        0x62773130,
        payload_len,
        payload_len,
        0x0100,
        samples_per_block,
    )
    pad_len = (-len(block)) % 8
    return header + block + (b"\x00" * pad_len)


def talk_binary_payload(adpcm_bytes: bytes, full_block_size: int, blocks_per_payload: int = 4) -> list[tuple[bytes, int]]:
    out: list[tuple[bytes, int]] = []
    blocks = [adpcm_bytes[i : i + full_block_size] for i in range(0, len(adpcm_bytes), full_block_size)]
    if blocks and len(blocks[-1]) != full_block_size:
        blocks = blocks[:-1]
    for i in range(0, len(blocks), blocks_per_payload):
        group = blocks[i : i + blocks_per_payload]
        payload = b"".join(bcmedia_adpcm_packet(b) for b in group)
        out.append((payload, len(group)))
    return out


def talk_binary_payload_custom(
    adpcm_bytes: bytes,
    *,
    full_block_size: int,
    blocks_per_payload: int,
    bcmedia_mode: str,
) -> list[tuple[bytes, int]]:
    def _packet(block: bytes) -> bytes:
        if len(block) < 5:
            raise ValueError("ADPCM block too small")
        payload_len = len(block) + 4

        if bcmedia_mode == "samples":
            val = (len(block) - 4) * 2 + 1
        elif bcmedia_mode == "bytes_half":
            # Older guess: bytes (excluding 4-byte wav header) / 2
            val = ((len(block) - 4) // 2)
        elif bcmedia_mode == "bytes":
            val = (len(block) - 4)
        else:
            raise ValueError(f"Unknown bcmedia_mode={bcmedia_mode!r}")

        header = struct.pack("<IHHHH", 0x62773130, payload_len, payload_len, 0x0100, int(val))
        pad_len = (-len(block)) % 8
        return header + block + (b"\x00" * pad_len)

    out: list[tuple[bytes, int]] = []
    blocks = [adpcm_bytes[i : i + full_block_size] for i in range(0, len(adpcm_bytes), full_block_size)]
    if blocks and len(blocks[-1]) != full_block_size:
        blocks = blocks[:-1]
    for i in range(0, len(blocks), blocks_per_payload):
        group = blocks[i : i + blocks_per_payload]
        payload = b"".join(_packet(b) for b in group)
        out.append((payload, len(group)))
    return out


async def ffmpeg_to_adpcm_wav(input_bytes: bytes, *, sample_rate: int, block_size: int, volume: float = 1.0) -> bytes:
    raise RuntimeError("ffmpeg_to_adpcm_wav is deprecated; use ffmpeg_to_pcm_s16le + ima_adpcm_encode_dvi_blocks")


async def ffmpeg_to_pcm_s16le(input_bytes: bytes, *, sample_rate: int, volume: float = 1.0) -> bytes:
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-af",
        f"volume={max(0.0, float(volume))}",
        "-ac",
        "1",
        "-ar",
        str(int(sample_rate)),
        "-f",
        "s16le",
        "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin and proc.stdout
    out, err = await proc.communicate(input_bytes)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {err.decode('utf-8', 'ignore')}")
    return out


_IMA_INDEX_TABLE: Final[list[int]] = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]
_IMA_STEP_TABLE: Final[list[int]] = [
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    16,
    17,
    19,
    21,
    23,
    25,
    28,
    31,
    34,
    37,
    41,
    45,
    50,
    55,
    60,
    66,
    73,
    80,
    88,
    97,
    107,
    118,
    130,
    143,
    157,
    173,
    190,
    209,
    230,
    253,
    279,
    307,
    337,
    371,
    408,
    449,
    494,
    544,
    598,
    658,
    724,
    796,
    876,
    963,
    1060,
    1166,
    1282,
    1411,
    1552,
    1707,
    1878,
    2066,
    2272,
    2499,
    2749,
    3024,
    3327,
    3660,
    4026,
    4428,
    4871,
    5358,
    5894,
    6484,
    7132,
    7845,
    8630,
    9493,
    10442,
    11487,
    12635,
    13899,
    15289,
    16818,
    18500,
    20350,
    22385,
    24623,
    27086,
    29794,
    32767,
]


def _ima_encode_nibble(sample: int, predictor: int, step_index: int) -> tuple[int, int, int]:
    step = _IMA_STEP_TABLE[step_index]
    diff = sample - predictor
    sign = 0
    if diff < 0:
        sign = 8
        diff = -diff

    delta = 0
    vpdiff = step >> 3
    if diff >= step:
        delta |= 4
        diff -= step
        vpdiff += step
    if diff >= (step >> 1):
        delta |= 2
        diff -= step >> 1
        vpdiff += step >> 1
    if diff >= (step >> 2):
        delta |= 1
        vpdiff += step >> 2

    predictor = predictor - vpdiff if sign else predictor + vpdiff
    predictor = max(-32768, min(32767, predictor))

    step_index += _IMA_INDEX_TABLE[delta | sign]
    step_index = max(0, min(88, step_index))

    return (delta | sign) & 0xF, predictor, step_index


def ima_adpcm_encode_dvi_blocks(pcm_s16le: bytes, *, full_block_size: int) -> bytes:
    """Encode PCM s16le into DVI-4 ADPCM blocks (streaming-state).

    Block layout:
    - 2 bytes: predictor (i16 LE)
    - 1 byte: step index
    - 1 byte: reserved (0)
    - (full_block_size - 4) bytes: packed IMA ADPCM nibbles
    """
    if full_block_size < 8:
        raise ValueError("full_block_size too small")
    if len(pcm_s16le) % 2 != 0:
        raise ValueError("PCM length must be even (s16le)")

    payload_bytes = full_block_size - 4
    payload_samples = payload_bytes * 2

    sample_count = len(pcm_s16le) // 2
    samples = struct.unpack("<" + ("h" * sample_count), pcm_s16le) if sample_count else ()
    if not samples:
        return b""

    predictor = int(samples[0])
    step_index = 0
    pos = 1  # predictor sample is implied by the block header

    out = bytearray()
    while pos <= len(samples):
        block = bytearray()
        block += struct.pack("<hBB", predictor, step_index, 0)

        nibble_acc = None
        for _ in range(payload_samples):
            s = int(samples[pos]) if pos < len(samples) else 0
            pos += 1
            nib, predictor, step_index = _ima_encode_nibble(s, predictor, step_index)
            if nibble_acc is None:
                nibble_acc = nib
            else:
                block.append((nibble_acc & 0xF) | ((nib & 0xF) << 4))
                nibble_acc = None
        if nibble_acc is not None:
            block.append(nibble_acc & 0xF)

        if len(block) < full_block_size:
            block.extend(b"\x00" * (full_block_size - len(block)))
        out += block[:full_block_size]

        if pos >= len(samples):
            break

    return bytes(out)


async def send_talk_binary(bc, *, channel: int, binary_payload: bytes, enc_type, mess_id: int | None = None) -> None:
    from reolink_aio.baichuan import util as bc_util
    from Crypto.Cipher import AES

    if not getattr(bc, "_logged_in", False):
        await bc.login()
    if not hasattr(bc, "_mess_id"):
        setattr(bc, "_mess_id", 0)

    ch_id = channel + 1
    ext = (
        BC_XML_HEADER
        + '<Extension version="1.1">\n'
        + "<binaryData>1</binaryData>\n"
        + f"<channelId>{channel}</channelId>\n"
        + "</Extension>\n"
    )

    if mess_id is None:
        bc._mess_id = (bc._mess_id + 1) % 16777216
    else:
        bc._mess_id = mess_id

    if enc_type == bc_util.EncType.BC:
        enc_ext = bc_util.encrypt_baichuan(ext, ch_id)
    else:
        enc_ext = bc._aes_encrypt(ext.encode("utf-8"))

    payload_offset = len(enc_ext)
    mess_len = payload_offset + len(binary_payload)

    cmd_id = 202
    header = (
        bytes.fromhex(bc_util.HEADER_MAGIC)
        + int(cmd_id).to_bytes(4, "little")
        + int(mess_len).to_bytes(4, "little")
        + int(ch_id).to_bytes(1, "little")
        + int(bc._mess_id).to_bytes(3, "little")
        + BC_MESSAGE_CLASS_1464
        + int(payload_offset).to_bytes(4, "little")
    )

    packet = header + enc_ext + binary_payload
    LOG.debug(
        "cmd202 write: host=%s ch=%s enc=%s mess_id=%s enc_ext=%s payload=%s mess_len=%s payload_offset=%s",
        getattr(bc, "_host", "?"),
        channel,
        getattr(enc_type, "value", str(enc_type)),
        getattr(bc, "_mess_id", "?"),
        len(enc_ext),
        len(binary_payload),
        mess_len,
        payload_offset,
    )

    # Wait for camera ack like neolink (firmwares can drop packets otherwise).
    await bc._connect_if_needed()
    proto = getattr(bc, "_protocol", None)
    loop = getattr(bc, "_loop", None)

    if proto is None or loop is None:
        async with bc._login_mutex:
            bc._connection._transport.write(packet)
        return

    full_mess_id = int.from_bytes(int(ch_id).to_bytes(1, "little") + int(bc._mess_id).to_bytes(3, "little"), "little")
    receive_future = loop.create_future()
    proto.receive_futures.setdefault(cmd_id, {})[full_mess_id] = receive_future

    try:
        async with bc._login_mutex:
            bc._connection._transport.write(packet)
        # Ack can be slow on some doorbells.
        await asyncio.wait_for(receive_future, timeout=5.0)
    finally:
        try:
            if not receive_future.done():
                receive_future.cancel()
        except Exception:
            pass
        futs = proto.receive_futures.get(cmd_id, {})
        futs.pop(full_mess_id, None)
        if not futs and cmd_id in proto.receive_futures:
            proto.receive_futures.pop(cmd_id, None)


async def send_talk_binary_with_encryptlen(
    bc,
    *,
    channel: int,
    binary_payload: bytes,
    enc_type,
    encrypt_payload_len: int,
    mess_id: int | None = None,
) -> None:
    """Send cmd202 with an Extension including <encryptLen> and AES-encrypt the first encrypt_payload_len bytes of payload.

    Rationale: reolink_aio's push parser supports payloads that include an <encryptLen>
    field to indicate a prefix of the payload is AES-encrypted. Some firmwares may
    require this for talk payloads too.
    """
    from reolink_aio.baichuan import util as bc_util
    from Crypto.Cipher import AES

    if not getattr(bc, "_logged_in", False):
        await bc.login()
    if not hasattr(bc, "_mess_id"):
        setattr(bc, "_mess_id", 0)

    ch_id = channel + 1
    encrypt_payload_len = max(0, min(int(encrypt_payload_len), len(binary_payload)))

    ext = (
        BC_XML_HEADER
        + '<Extension version="1.1">\n'
        + "<binaryData>1</binaryData>\n"
        + f"<channelId>{channel}</channelId>\n"
        + f"<encryptLen>{encrypt_payload_len}</encryptLen>\n"
        + "</Extension>\n"
    )

    if mess_id is None:
        bc._mess_id = (bc._mess_id + 1) % 16777216
    else:
        bc._mess_id = mess_id

    if enc_type == bc_util.EncType.BC:
        enc_ext = bc_util.encrypt_baichuan(ext, ch_id)
        # If we're using BC XML encryption, we still AES-encrypt the payload prefix.
    else:
        enc_ext = bc._aes_encrypt(ext.encode("utf-8"))

    # AES-encrypt payload prefix (same mode/IV as Baichuan AES XML).
    if encrypt_payload_len > 0:
        if getattr(bc, "_aes_key", None) is None:
            raise RuntimeError("Baichuan AES key not available (did login succeed?)")
        cipher = AES.new(key=bc._aes_key, mode=AES.MODE_CFB, iv=bc_util.AES_IV, segment_size=128)
        enc_prefix = cipher.encrypt(binary_payload[:encrypt_payload_len])
        binary_payload = enc_prefix + binary_payload[encrypt_payload_len:]

    payload_offset = len(enc_ext)
    mess_len = payload_offset + len(binary_payload)

    cmd_id = 202
    header = (
        bytes.fromhex(bc_util.HEADER_MAGIC)
        + int(cmd_id).to_bytes(4, "little")
        + int(mess_len).to_bytes(4, "little")
        + int(ch_id).to_bytes(1, "little")
        + int(bc._mess_id).to_bytes(3, "little")
        + BC_MESSAGE_CLASS_1464
        + int(payload_offset).to_bytes(4, "little")
    )

    packet = header + enc_ext + binary_payload
    LOG.debug(
        "cmd202 write (encryptLen=%s): host=%s ch=%s enc=%s mess_id=%s enc_ext=%s payload=%s mess_len=%s payload_offset=%s",
        encrypt_payload_len,
        getattr(bc, "_host", "?"),
        channel,
        getattr(enc_type, "value", str(enc_type)),
        getattr(bc, "_mess_id", "?"),
        len(enc_ext),
        len(binary_payload),
        mess_len,
        payload_offset,
    )

    await bc._connect_if_needed()
    async with bc._login_mutex:
        bc._connection._transport.write(packet)


async def run(args: argparse.Namespace) -> int:
    from reolink_aio.api import Host
    from reolink_aio.baichuan import util as bc_util
    from reolink_aio.exceptions import ApiError

    cfg = load_reolink_entry(title=args.title, host=args.host)
    if cfg is None:
        LOG.error("No Reolink config entry found. Use --list, or pass --title/--host.")
        return 2

    LOG.info("Target: title=%s host=%s http_port=%s use_https=%s bc_port=%s user=%s", cfg["title"], cfg["host"], cfg["port"], cfg["use_https"], cfg["baichuan_port"], cfg["username"])

    # Load audio input bytes.
    if args.sine is not None:
        input_bytes = generate_sine_wav(freq_hz=int(args.sine), duration_s=float(args.duration), sample_rate=16000)
        LOG.info("Generated sine wav: %s Hz, %s s", args.sine, args.duration)
    else:
        p = Path(args.file)
        if not p.is_absolute():
            p = ROOT / p
        input_bytes = p.read_bytes()
        LOG.info("Loaded file: %s (%s bytes)", p, len(input_bytes))

    # aiohttp session callback expected by reolink_aio
    import aiohttp

    session = aiohttp.ClientSession()
    try:
        host = Host(
            host=cfg["host"],
            username=cfg["username"],
            password=cfg["password"],
            port=cfg["port"],
            use_https=cfg["use_https"],
            bc_port=cfg["baichuan_port"],
            aiohttp_get_session_callback=lambda: session,
        )
        bc = host.baichuan

        await bc.login()
        ability_xml = await bc.send(cmd_id=10, channel=int(args.channel))
        ability = parse_talk_ability(ability_xml)
        LOG.info(
            "TalkAbility: audioType=%s sampleRate=%s precision=%s lengthPerEncoder=%s duplex=%s streamMode=%s track=%s",
            ability.audio_type,
            ability.sample_rate,
            ability.sample_precision,
            ability.length_per_encoder,
            ability.duplex,
            ability.audio_stream_mode,
            ability.sound_track,
        )

        if ability.audio_type.lower() != "adpcm":
            LOG.error("Unsupported audioType=%s (this harness only supports ADPCM for now)", ability.audio_type)
            return 3

        # Optional overrides for experimentation.
        if args.override_block_size is not None:
            ability = TalkAbility(
                duplex=ability.duplex,
                audio_stream_mode=ability.audio_stream_mode,
                audio_type=ability.audio_type,
                priority=ability.priority,
                sample_rate=ability.sample_rate,
                sample_precision=ability.sample_precision,
                length_per_encoder=int(args.override_block_size),
                sound_track=ability.sound_track,
            )
            LOG.info("Override: lengthPerEncoder=%s", ability.length_per_encoder)
        if args.override_stream_mode is not None:
            ability = TalkAbility(
                duplex=ability.duplex,
                audio_stream_mode=str(args.override_stream_mode),
                audio_type=ability.audio_type,
                priority=ability.priority,
                sample_rate=ability.sample_rate,
                sample_precision=ability.sample_precision,
                length_per_encoder=ability.length_per_encoder,
                sound_track=ability.sound_track,
            )
            LOG.info("Override: audioStreamMode=%s", ability.audio_stream_mode)

        # Transcode using ability's sample rate and block size.
        full_block = (int(ability.length_per_encoder) // 2) + 4
        pcm = await ffmpeg_to_pcm_s16le(input_bytes, sample_rate=ability.sample_rate, volume=float(args.volume))
        adpcm = ima_adpcm_encode_dvi_blocks(pcm, full_block_size=full_block)
        LOG.info("ADPCM DVI: full_block=%s data_bytes=%s blocks=%s", full_block, len(adpcm), len(adpcm) // full_block)

        # Try sending TalkConfig cmd 201 with AES then BC, and with XML variants.
        # Some firmwares/cameras reject cmd 201 if a previous talk session is still
        # active; in that case, sending cmd 11 (stop talk) then retrying can help.
        enc_used = None
        sent_cfg = None
        for cfg_xml in build_talk_config_variants(int(args.channel), ability):
            for enc in (bc_util.EncType.AES, bc_util.EncType.BC):
                try:
                    await bc.send(cmd_id=201, channel=int(args.channel), body=cfg_xml, enc_type=enc)
                    enc_used = enc
                    sent_cfg = cfg_xml
                    LOG.info("TalkConfig accepted: enc=%s variant_len=%s", enc.value, len(cfg_xml))
                    break
                except ApiError as e:
                    rsp = getattr(e, "rspCode", None)
                    LOG.debug("TalkConfig rejected: enc=%s rspCode=%s variant_len=%s", enc.value, rsp, len(cfg_xml))
                    if rsp in (400, 422):
                        # Best-effort: stop talk and retry once.
                        try:
                            await bc.send(cmd_id=11, channel=int(args.channel), enc_type=enc)
                            await asyncio.sleep(0.1)
                            await bc.send(cmd_id=201, channel=int(args.channel), body=cfg_xml, enc_type=enc)
                            enc_used = enc
                            sent_cfg = cfg_xml
                            LOG.info("TalkConfig accepted after stop/retry: enc=%s variant_len=%s", enc.value, len(cfg_xml))
                            break
                        except Exception:
                            pass
                    continue
            if enc_used is not None:
                break

        if enc_used is None:
            LOG.error("TalkConfig cmd 201 rejected for all variants/encryption modes.")
            return 4

        # Send cmd 202 payloads paced similarly to the HA integration.
        payloads = talk_binary_payload_custom(
            adpcm,
            full_block_size=full_block,
            blocks_per_payload=int(args.blocks_per_payload),
            bcmedia_mode=str(args.bcmedia_mode),
        )
        LOG.info("Sending cmd202 payloads: count=%s blocks_per_payload=%s", len(payloads), args.blocks_per_payload)

        start = time.time()
        for i, (payload, blocks_in_payload) in enumerate(payloads, 1):
            if args.encrypt_payload_len is not None:
                enc_len = len(payload) if args.encrypt_payload_len == "all" else int(args.encrypt_payload_len)
                await send_talk_binary_with_encryptlen(
                    bc,
                    channel=int(args.channel),
                    binary_payload=payload,
                    enc_type=enc_used,
                    encrypt_payload_len=enc_len,
                )
            else:
                await send_talk_binary(bc, channel=int(args.channel), binary_payload=payload, enc_type=enc_used)
            adpcm_len = full_block * blocks_in_payload
            samples_sent = (adpcm_len - 4 * blocks_in_payload) * 2 + blocks_in_payload
            play_len = samples_sent / float(ability.sample_rate)
            await asyncio.sleep(play_len)
            if i % 25 == 0 or i == len(payloads):
                LOG.info("Progress: %s/%s (elapsed %.2fs)", i, len(payloads), time.time() - start)

        # Stop talk (cmd 11) best-effort.
        try:
            await bc.send(cmd_id=11, channel=int(args.channel), enc_type=enc_used)
        except Exception as e:
            LOG.warning("cmd11 stop failed (ignored): %s", e)

        LOG.info("Done. If you heard audio: we have a working framing for this camera.")
        return 0
    finally:
        await session.close()


def generate_sine_wav(*, freq_hz: int, duration_s: float, sample_rate: int = 16000) -> bytes:
    """Generate a mono 16-bit PCM WAV containing a sine wave."""
    import math

    n = int(sample_rate * duration_s)
    amp = 0.25  # avoid clipping

    pcm = bytearray()
    for i in range(n):
        t = i / sample_rate
        v = amp * math.sin(2 * math.pi * freq_hz * t)
        s = int(max(-1.0, min(1.0, v)) * 32767.0)
        pcm += struct.pack("<h", s)

    # RIFF/WAVE header
    num_channels = 1
    bits = 16
    byte_rate = sample_rate * num_channels * (bits // 8)
    block_align = num_channels * (bits // 8)
    data_len = len(pcm)

    out = bytearray()
    out += b"RIFF"
    out += struct.pack("<I", 36 + data_len)
    out += b"WAVE"
    out += b"fmt "
    out += struct.pack("<I", 16)  # PCM fmt chunk size
    out += struct.pack("<HHIIHH", 1, num_channels, sample_rate, byte_rate, block_align, bits)
    out += b"data"
    out += struct.pack("<I", data_len)
    out += pcm
    return bytes(out)


def load_reolink_entry(*, title: str | None, host: str | None) -> dict | None:
    entries = list_reolink_entries()
    if host:
        for e in entries:
            if e["host"] == host:
                return e
    if title:
        title_l = title.lower()
        for e in entries:
            if (e["title"] or "").lower() == title_l:
                return e
        # partial match
        for e in entries:
            if title_l in (e["title"] or "").lower():
                return e
    return entries[0] if entries else None


def list_reolink_entries() -> list[dict]:
    storage_dir = ROOT / ".storage"

    def _load_core_config_entries() -> dict:
        # HA stores config entries in JSON under key "core.config_entries".
        # Some installs can have a corrupted/zero-padded file; scan backups too.
        candidates: list[Path] = []
        for pat in ("core.config_entries", "core.config_entries.bak_*", "core.config_entries.*"):
            candidates.extend(sorted(storage_dir.glob(pat)))
        seen = set()
        for p in candidates:
            if p in seen or not p.is_file():
                continue
            seen.add(p)
            try:
                raw = p.read_bytes().rstrip(b"\x00")
                txt = raw.decode("utf-8", "strict")
                obj = json.loads(txt)
            except Exception:
                continue
            if obj.get("key") == "core.config_entries":
                return obj
        raise FileNotFoundError("No valid .storage/core.config_entries JSON found (key=core.config_entries)")

    obj = _load_core_config_entries()
    out = []
    for e in obj.get("data", {}).get("entries", []):
        if e.get("domain") != "reolink":
            continue
        d = e.get("data", {})
        out.append(
            {
                "title": e.get("title"),
                "entry_id": e.get("entry_id"),
                "host": d.get("host"),
                "port": d.get("port"),
                "use_https": d.get("use_https"),
                "baichuan_port": d.get("baichuan_port", 9000),
                "username": d.get("username"),
                "password": d.get("password"),
            }
        )
    return out


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="List Reolink config entries from .storage/core.config_entries")
    ap.add_argument("--title", help="Reolink config entry title to use (e.g. 'Deurbel')")
    ap.add_argument("--host", help="Camera IP to use (overrides --title)")
    ap.add_argument("--channel", type=int, default=0, help="Camera channel index (default: 0)")
    ap.add_argument("--file", default="media/doorbell.mp3", help="Audio file to play (default: media/doorbell.mp3, relative to HA config root)")
    ap.add_argument("--sine", type=int, help="Generate sine tone at this frequency (Hz) instead of reading --file")
    ap.add_argument("--duration", type=float, default=2.0, help="Sine tone duration (seconds)")
    ap.add_argument("--volume", type=float, default=1.0, help="Software volume multiplier before ADPCM encoding")
    ap.add_argument("--blocks-per-payload", type=int, default=4, help="ADPCM blocks grouped into one cmd202 payload (default: 4)")
    ap.add_argument(
        "--bcmedia-mode",
        default="bytes_half",
        choices=["samples", "bytes_half", "bytes"],
        help="How to fill the BcMedia ADPCM header block field (default: bytes_half, per neolink)",
    )
    ap.add_argument("--override-block-size", type=int, help="Override TalkAbility lengthPerEncoder/block_size (e.g. 512)")
    ap.add_argument("--override-stream-mode", help="Override audioStreamMode (e.g. mixAudioStream)")
    ap.add_argument(
        "--encrypt-payload-len",
        dest="encrypt_payload_len",
        help="If set, include <encryptLen> in cmd202 Extension and AES-encrypt that many bytes of payload (use 'all' for full payload).",
    )
    ap.add_argument("--debug", action="store_true", help="Enable debug logging")
    return ap.parse_args(argv)


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level=logging.DEBUG if "--debug" in argv else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _ensure_venv()
    args = parse_args(argv)

    if args.list:
        entries = list_reolink_entries()
        print("Reolink entries:")
        for e in entries:
            print(f"- title={e['title']!r} host={e['host']!r} port={e['port']!r} use_https={e['use_https']!r} bc_port={e['baichuan_port']!r} entry_id={e['entry_id']!r}")
        return 0

    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
