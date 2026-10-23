// Inline push-to-talk button for the "reolink_talk" live WebSocket relay.
// Designed to be dropped in as a picture-elements custom element on top of
// an existing (already-playing) camera card, so it does NOT open a second
// video connection -- it only opens the audio WebSocket while held down.
//
// Usage inside an advanced-camera-card (or any picture-elements-capable card):
//
//   elements:
//     - type: custom:reolink-talk-button
//       camera: front_door   # slugified Reolink camera name, e.g. "Front Door" -> front_door
//       style:
//         bottom: 15px
//         left: 10%   # avoids overlapping advanced-camera-card's PTZ controls
//
// The camera value must match the slug of one of your Reolink camera names
// (the same name used to build the "Reolink Talk <name>" media_player entity
// this integration creates). If unsure, connect once with any value -- the
// resulting error lists every valid slug.
//
// Auth: before opening the audio socket, a short-lived token is requested
// over Home Assistant's own authenticated WebSocket API. The audio socket
// rejects any connection without a valid token.

class ReolinkTalkButton extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    this._camera = this._config.camera || "";
    if (!this._camera) {
      console.error("reolink-talk-button: missing required `camera:` config value");
    }
    this._recording = false;
    this._wsReady = false;
    this._cooldown = false;
    if (this.isConnected) {
      this._render();
    }
  }
  connectedCallback() {
    if (!this._btn) {
      this._render();
    }
  }

  set hass(hass) {
    this._hass = hass;
  }

  getCardSize() {
    return 1;
  }

  _render() {
    if (this._btn) return;

    // An inline SVG (not an emoji) so the icon looks identical on every
    // platform -- emoji glyph rendering for U+1F399 varies wildly across
    // Android/iOS WebView versions and can show up as an ugly fallback glyph.
    // Built as a single line with no surrounding whitespace: a template
    // literal spanning multiple lines leaves text nodes as siblings of the
    // button, and hosts that walk childNodes (rather than children) will
    // hand those text nodes to getComputedStyle and throw.
    const micSvg = '<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true"><path d="M12,2A3,3 0 0,1 15,5V11A3,3 0 0,1 12,14A3,3 0 0,1 9,11V5A3,3 0 0,1 12,2M19,11C19,14.53 16.39,17.44 13,17.93V21H11V17.93C7.61,17.44 5,14.53 5,11H7A5,5 0 0,0 12,16A5,5 0 0,0 17,11H19Z" /></svg>';
    const btnStyle = 'width:48px;height:48px;border-radius:50%;border:none;background:rgba(0,0,0,0.5);color:#fff;line-height:1;display:flex;align-items:center;justify-content:center;touch-action:none;-webkit-user-select:none;user-select:none;cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,0.4);transition:background 0.1s ease,transform 0.1s ease;';
    this.innerHTML = '<button id="rtb-btn" style="' + btnStyle + '">' + micSvg + '</button>';
    this._btn = this.querySelector("#rtb-btn");
    this._btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (this._cooldown) return;
      if (!this._camera) {
        console.error("reolink-talk-button: no camera configured yet");
        return;
      }
      if (this._recording) {
        this._stop();
      } else {
        this._start();
      }
    });
  }

  _setVisualState(state) {
    if (!this._btn) return;
    const colors = {
      idle: "rgba(0,0,0,0.5)",
      connecting: "rgba(58,123,253,0.85)",
      live: "rgba(220,40,40,0.9)",
      error: "rgba(180,0,0,0.9)",
    };
    this._btn.style.background = colors[state] || colors.idle;
    this._btn.style.transform = state === "live" ? "scale(1.08)" : "scale(1)";
  }

  _failWith(message, err) {
    console.error("reolink-talk-button: " + message, err || "");
    this._report(message, err);
    this._setVisualState("error");
    this._recording = false;
    setTimeout(() => this._setVisualState("idle"), 1500);
  }

  // A failure is otherwise visible only as a red flash, and a phone app has no
  // console to read. Show the reason as a Home Assistant toast, and send it to
  // the integration, which keeps the last one in
  // sensor.reolink_talk_last_client_error.
  _report(message, err) {
    let detail = "";
    if (err) {
      detail = err.name || err.message ? (err.name || "Error") + ": " + (err.message || "") : String(err);
    }
    const text = "Reolink Talk (" + this._camera + "): " + message + (detail ? " - " + detail : "");
    this.dispatchEvent(new CustomEvent("hass-notification", {
      detail: { message: text }, bubbles: true, composed: true,
    }));
    try {
      if (this._hass && this._hass.connection) {
        this._hass.connection.sendMessagePromise({
          type: "reolink_talk/client_error",
          camera: String(this._camera).slice(0, 64),
          message: text.slice(0, 500),
          context: ("secure=" + window.isSecureContext + " mediaDevices=" + !!navigator.mediaDevices +
                    " ua=" + navigator.userAgent).slice(0, 500),
        }).catch(() => {});
      }
    } catch (e) {
      // reporting must never break the button
    }
  }

  // Ask Home Assistant -- over its own authenticated WebSocket connection --
  // for a short-lived token authorising a talk session on this camera.
  async _requestToken() {
    if (!this._hass || !this._hass.connection) {
      throw new Error("Home Assistant connection not available to this element");
    }
    const result = await this._hass.connection.sendMessagePromise({
      type: "reolink_talk/get_token",
      camera: this._camera,
    });
    if (!result || !result.token) {
      throw new Error("no token returned by reolink_talk/get_token");
    }
    return result.token;
  }

  async _start() {
    if (this._recording) return;
    this._recording = true;
    this._setVisualState("connecting");

    let token;
    try {
      token = await this._requestToken();
    } catch (err) {
      this._failWith("could not obtain talk token", err);
      return;
    }

    try {
      this._mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1,
        },
      });
    } catch (err) {
      this._failWith("mic error", err);
      return;
    }

    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    this._ws = new WebSocket(
      proto + "//" + location.host + "/api/reolink_talk/live_ws" +
      "?camera=" + encodeURIComponent(this._camera) +
      "&token=" + encodeURIComponent(token)
    );
    this._ws.binaryType = "arraybuffer";
    this._wsReady = false;

    this._ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.status === "ready") {
          this._wsReady = true;
          this._setVisualState("live");
        }
      } catch (e) {
        // ignore
      }
    };

    this._ws.onerror = () => {
      this._setVisualState("error");
    };

    this._ws.onclose = (ev) => {
      this._teardownAudio();
      if (this._recording) {
        this._report(this._wsReady ? "talk session closed" : "talk socket closed before ready",
                     { name: "close " + ev.code, message: ev.reason || "" });
        this._setVisualState("error");
        setTimeout(() => this._setVisualState("idle"), 1200);
      }
      this._recording = false;
    };

    this._ws.onopen = () => {
      this._audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      this._sourceNode = this._audioCtx.createMediaStreamSource(this._mediaStream);
      const bufferSize = 4096;
      this._processorNode = this._audioCtx.createScriptProcessor(bufferSize, 1, 1);

      this._processorNode.onaudioprocess = (e) => {
        if (!this._recording || !this._wsReady || this._ws.readyState !== WebSocket.OPEN) return;
        const input = e.inputBuffer.getChannelData(0);
        const pcm16 = this._downsampleBuffer(input, this._audioCtx.sampleRate, 16000);
        if (pcm16.length > 0) {
          this._ws.send(pcm16.buffer);
        }
      };

      this._sourceNode.connect(this._processorNode);
      const silentGain = this._audioCtx.createGain();
      silentGain.gain.value = 0;
      this._processorNode.connect(silentGain);
      silentGain.connect(this._audioCtx.destination);
    };
  }

  _stop() {
    if (!this._recording) return;
    this._recording = false;
    this._setVisualState("idle");
    this._teardownAudio();
    if (this._ws && this._ws.readyState === WebSocket.OPEN) {
      this._ws.close();
    }
    this._ws = null;
    this._cooldown = true;
    if (this._btn) this._btn.style.opacity = "0.5";
    setTimeout(() => {
      this._cooldown = false;
      if (this._btn) this._btn.style.opacity = "1";
    }, 1200);
  }

  _teardownAudio() {
    if (this._processorNode) {
      try { this._processorNode.disconnect(); } catch (e) {}
      this._processorNode.onaudioprocess = null;
      this._processorNode = null;
    }
    if (this._sourceNode) {
      try { this._sourceNode.disconnect(); } catch (e) {}
      this._sourceNode = null;
    }
    if (this._audioCtx) {
      try { this._audioCtx.close(); } catch (e) {}
      this._audioCtx = null;
    }
    if (this._mediaStream) {
      this._mediaStream.getTracks().forEach((t) => t.stop());
      this._mediaStream = null;
    }
  }

  _downsampleBuffer(buffer, inputSampleRate, outputSampleRate) {
    if (outputSampleRate === inputSampleRate) {
      return this._floatTo16BitPCM(buffer);
    }
    const ratio = inputSampleRate / outputSampleRate;
    const newLength = Math.round(buffer.length / ratio);
    const result = new Float32Array(newLength);
    let offsetResult = 0;
    let offsetBuffer = 0;
    while (offsetResult < result.length) {
      const nextOffsetBuffer = Math.round((offsetResult + 1) * ratio);
      let accum = 0, count = 0;
      for (let i = offsetBuffer; i < nextOffsetBuffer && i < buffer.length; i++) {
        accum += buffer[i];
        count++;
      }
      result[offsetResult] = count > 0 ? accum / count : 0;
      offsetResult++;
      offsetBuffer = nextOffsetBuffer;
    }
    return this._floatTo16BitPCM(result);
  }

  _floatTo16BitPCM(float32Array) {
    const out = new Int16Array(float32Array.length);
    for (let i = 0; i < float32Array.length; i++) {
      let s = Math.max(-1, Math.min(1, float32Array[i]));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return out;
  }

  disconnectedCallback() {
    this._stop();
  }
}

if (!customElements.get("reolink-talk-button")) {
  customElements.define("reolink-talk-button", ReolinkTalkButton);
}
