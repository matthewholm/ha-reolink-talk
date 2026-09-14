# ha-reolink-talk

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/v/release/matthewholm/ha-reolink-talk)](https://github.com/matthewholm/ha-reolink-talk/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/matthewholm/ha-reolink-talk/blob/main/LICENSE)

Two-way audio for Reolink cameras in Home Assistant, including cameras behind a Reolink Home Hub or NVR. Everything runs inside Home Assistant's own HTTP server, so there is no separate process, no extra port and no reverse proxy.

There are two ways to talk to a camera:

| Mode | What it is | Entity or element |
|------|-----------|-------------------|
| **One-shot playback** | Send TTS or an audio file to the camera speaker, like any other media player | `media_player.play_media` |
| **Live push-to-talk** | A mic button on your existing camera card that streams your voice in near real time | `custom:reolink-talk-button` |

This is a fork of [joeblack2k/reolink_talk](https://github.com/joeblack2k/reolink_talk) (MIT). That project's README notes that talk-back may not work when a camera sits behind an NVR or Home Hub. This fork adds multi-channel Home Hub support and a live streaming mode on top of the original's one-shot playback. See [Credits](#credits).

## Why this exists

Reolink's official two-way audio only works through the Reolink app. Getting it into Home Assistant has meant one of two things: ONVIF, which many Reolink models don't expose talk-back through, or running [neolink](https://github.com/QuantumEntangledAndy/neolink) as a separate RTSP bridge.

This integration speaks the Baichuan binary protocol directly from Home Assistant instead. The message framing was validated against neolink's reference implementation, and the ADPCM/BcMedia framing matches byte for byte.

## Features

* Native `HomeAssistantView` HTTP and WebSocket endpoints, registered on HA's own web server.
* Stateful IMA/DVI-4 ADPCM encoding in Python, matched to whatever the camera advertises in `TalkAbility` (`length_per_encoder`, sample rate).
* Config flow setup through the UI. No YAML, no IP addresses, no channel numbers to hand-edit.
* One `media_player` entity per camera channel for TTS and file playback.
* A Lovelace element, `reolink-talk-button`, served by the integration and registered as a Lovelace resource automatically. No manual file copying, no resource to add by hand.
* The element overlays a camera card you already have. Tapping it opens an audio WebSocket only, reusing the video connection the card already holds open.
* Automatic recovery from a talk session the camera never closed. Baichuan rejection codes 400, 421 and 422 trigger a stop and one retry.
* Client-side cooldown so rapid re-tapping doesn't race itself.

## Requirements

| Requirement | Notes |
|-------------|-------|
| [Reolink integration](https://www.home-assistant.io/integrations/reolink) | Already configured for your Home Hub or NVR. This integration reuses its credentials. |
| [`reolink-aio`](https://github.com/starkillerOG/reolink_aio) | Comes along automatically with HA's Reolink integration. |
| `ffmpeg` | Used to transcode audio for one-shot playback. Already in the official HA Docker and OS images. |
| A camera with talk support | Must advertise ADPCM as its talk codec. Most Reolink cameras with a speaker do. |
| [`advanced-camera-card`](https://github.com/dermotduffy/advanced-camera-card) | Only needed for the inline mic button, not for TTS. |

## Installation

### 1. Install

Pick one:
* **HACS** — add this repo as a custom repository (`https://github.com/matthewholm/ha-reolink-talk`, category **Integration**), then install **Reolink Talk (Two-Way Audio + Live Talk)**.

* **Manually** — copy `custom_components/reolink_talk/` into your `config/custom_components/` directory

### 2. Restart Home Assistant

### 3. Add the integration

Go to **Settings → Devices & Services → Add Integration**, search for **Reolink Talk**, and pick the Reolink config entry it should attach to. It discovers every camera on that entry by itself.

That's it. The Lovelace element is served and registered by the integration, so there is nothing to copy into `www/` and no resource to add under Dashboards.

## Configuration

There is one setting: which Reolink config entries this integration pulls cameras from. Find it under **Settings → Devices & Services → Reolink Talk → Configure**. It uses all of them by default, so this only matters if you run more than one Home Hub or NVR and want to limit which ones get talk entities.

## Usage

### One-shot playback

Find your entity first: **Settings → Devices & Services → Entities**, search for "talk". You'll get something like `media_player.reolink_talk_front_door`.

```yaml
service: media_player.play_media
target:
  entity_id: media_player.reolink_talk_<your_camera>
data:
  media_content_id: "Someone is at the door"
  media_content_type: music
```

Any HA TTS engine works through the normal `tts.speak` and `media_player.play_media` flow.

### Live push-to-talk

Add the element as a picture element on top of an existing camera card:

```yaml
type: custom:advanced-camera-card
cameras:
  - camera_entity: camera.your_camera
elements:
  - type: custom:reolink-talk-button
    camera: front_door
    style:
      bottom: 15px
      left: 10%
```

Tap once to start, tap again to stop. The button changes colour for idle, connecting, live and error.

Bottom left keeps the button clear of `advanced-camera-card`'s PTZ controls, which usually sit centred at the bottom. Adjust `bottom` and `left` to suit your own layout.

**Finding the right `camera:` value.** It's the slugified name your Reolink hub reports, the same one used to build `media_player.reolink_talk_<name>`. A camera called "Front Door" becomes `front_door`. If you don't know yours, put any placeholder in there and tap the button once. The connection fails, and **Settings → System → Logs** (search `reolink_talk`) lists every valid slug for your setup.

## How it works

### Protocol

Talk sessions are negotiated over these Baichuan command IDs:

| Command | Purpose |
|---------|---------|
| `10` | Query `TalkAbility` |
| `201` | `TalkConfig`, starts the session |
| `202` | Binary ADPCM audio payload |
| `11` | Stop |

### Audio path

Audio is encoded as stateful IMA/DVI-4 ADPCM in full blocks sized from the camera's advertised `length_per_encoder`, then wrapped in the Reolink SDK's `01wb` talk-stream framing and sent as `202` payloads.

For live talk, the browser captures mic audio via `getUserMedia` and `AudioContext`, downsamples to 16 kHz mono PCM16, and streams it over a WebSocket to `/api/reolink_talk/live_ws`. That endpoint encodes each block to ADPCM and forwards it to the camera as it arrives.

### Error recovery

A `421` from `TalkConfig` (or a `400` or `422`) means a previous session on that channel wasn't torn down cleanly. The integration sends a stop (`cmd 11`) and retries once.

## Troubleshooting

| Symptom | Cause |
|---------|-------|
| The button doesn't render at all | Cached frontend. Hard-reload the browser, or clear the frontend cache in the Companion App. |
| The button shows "Configuration error" after a hard reload on iOS | The element didn't finish registering before the card rendered. Check that `/reolink_talk/reolink-talk-button.js` is listed under **Settings → Dashboards → ⋮ → Resources**, then clear the frontend cache in the Companion App. |
| "unknown camera" in the WebSocket response | Wrong slug in `camera:`. The error message lists every valid one. |
| "undefined is not an object (evaluating 'navigator.mediaDevices.getUserMedia')" | Not a valid HTTPS origin. See [Limitations](#limitations). |
| Talk fails with a `421` and doesn't recover | The camera has a stuck session. Restart the camera or the hub. |
| `404` on `/api/reolink_talk/live_ws` | The integration isn't loaded. Check **Settings → Devices & Services** for errors. |

To check that the endpoint is reachable:

```bash
curl -i -N -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  https://your-ha-hostname/api/reolink_talk/live_ws
```

`101 Switching Protocols` means it's live. Note that this also demonstrates the endpoint is unauthenticated, see below.

## Authentication

The audio WebSocket can't use Home Assistant's normal bearer-token auth, because the browser's WebSocket API can't set request headers. Instead, the Lovelace element requests a short-lived single-use token over Home Assistant's *authenticated* WebSocket API (`reolink_talk/get_token`) and presents it when opening the audio socket. Tokens expire after 30 seconds, are bound to one camera, and are consumed on use.

An unauthenticated caller gets a `401` and learns nothing — not even which cameras exist. You can verify this on your own instance:

```bash
curl -i -N -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  https://your-ha-hostname/api/reolink_talk/live_ws
```

## Limitations

**Push-to-talk needs genuinely valid HTTPS.** Browsers only expose `navigator.mediaDevices.getUserMedia` on pages served over HTTPS with a certificate valid for the exact hostname you connected to. If you reach Home Assistant on a raw IP such as `https://192.168.1.50:8123` while your certificate was issued for a domain name, most browsers and WebViews, Companion App included, disable `navigator.mediaDevices` entirely. The mic button then fails instantly with "undefined is not an object (evaluating 'navigator.mediaDevices.getUserMedia')".
 
This can't be fixed in application code, it's a browser security boundary. Set both your Internal URL and External URL to the same valid HTTPS hostname, never a raw IP.
 
**Tested on a narrow setup.** A Reolink Home Hub with battery cameras advertising ADPCM talk support. Other hub or NVR models, or other codecs, may need adjustment.

**Some duplicated logic.** The one-shot and live paths each carry their own TalkConfig retry handling. Worth unifying at some point.

## What's different from the original

* Home Hub and NVR multi-channel support with automatic camera discovery. No source edits, no IP addresses to configure. This addresses the limitation the original project called out.
* Live streaming push-to-talk over WebSocket, rather than one-shot playback only.
* The `reolink-talk-button` Lovelace element, served and registered by the integration, overlaying an existing card without opening a second video connection.
* Automatic recovery from stuck talk sessions, plus a cooldown against rapid re-tap races.

## Credits

* [joeblack2k/reolink_talk](https://github.com/joeblack2k/reolink_talk), the project this is forked from. It introduced the `media_player` one-shot approach and the `TalkAbility`/ADPCM handling for standalone cameras.
* [neolink](https://github.com/QuantumEntangledAndy/neolink), the reference implementation used to validate Baichuan and BcMedia framing.
* [reolink_aio](https://github.com/starkillerOG/reolink_aio), the Baichuan client library underneath all of this, also used by HA's core Reolink integration.
* [advanced-camera-card](https://github.com/dermotduffy/advanced-camera-card), the card the talk button overlays.

## License

MIT, see [LICENSE](https://github.com/matthewholm/ha-reolink-talk/blob/main/LICENSE). Inherited from [joeblack2k/reolink_talk](https://github.com/joeblack2k/reolink_talk).
