from __future__ import annotations

import hashlib
import logging

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_CHANNEL, CONF_REOLINK_ENTRY_IDS, DEFAULT_CHANNEL, DOMAIN
from .talk_live_view import async_register_views

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["media_player"]

FRONTEND_URL = "/reolink_talk/reolink-talk-button.js"
FRONTEND_FILE = "frontend/reolink-talk-button.js"


async def _async_register_lovelace_resource(hass: HomeAssistant) -> None:
    """Register the element as a real Lovelace resource.

    add_extra_js_url() injects the script as a deferred module, which means a
    card can finish rendering before customElements.define() has run -- the
    card then reports "Custom element doesn't exist". Lovelace resources, by
    contrast, are awaited before dashboards render, so registering there
    removes the race entirely.
    """
    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        return

    resources = getattr(lovelace, "resources", None)
    if resources is None:
        return

    try:
        if not resources.loaded:
            await resources.async_load()
            resources.loaded = True
    except Exception:  # storage-mode only; YAML dashboards manage their own
        _LOGGER.debug("Lovelace resources unavailable, falling back to extra_js_url")
        return

    # The file is served with a month-long cache header, so the URL carries a
    # hash of its content: a changed button gets a new URL and every client,
    # the companion app included, fetches it instead of running the old one.
    url = f"{FRONTEND_URL}?v={await hass.async_add_executor_job(_frontend_hash, hass)}"

    for item in resources.async_items():
        if item.get("url", "").split("?")[0] == FRONTEND_URL:
            if item["url"] != url:
                await resources.async_update_item(item["id"], {"url": url})
                _LOGGER.info("Updated Lovelace resource to %s", url)
            return

    await resources.async_create_item({"res_type": "module", "url": url})
    _LOGGER.info("Registered Lovelace resource %s", url)


def _frontend_hash(hass: HomeAssistant) -> str:
    path = hass.config.path(f"custom_components/{DOMAIN}/{FRONTEND_FILE}")
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the Lovelace element from the integration itself.

    Registered once per HA start, so multiple config entries don't try to
    claim the same static path.
    """
    if hass.data.setdefault(DOMAIN, {}).get("frontend_registered"):
        return

    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                FRONTEND_URL,
                hass.config.path(f"custom_components/{DOMAIN}/{FRONTEND_FILE}"),
                True,
            )
        ]
    )
    await _async_register_lovelace_resource(hass)
    hass.data[DOMAIN]["frontend_registered"] = True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Default options: all current Reolink config entries, channel 0.
    if not entry.options:
        reolink_entry_ids = [e.entry_id for e in hass.config_entries.async_entries("reolink")]
        hass.config_entries.async_update_entry(
            entry,
            options={
                CONF_REOLINK_ENTRY_IDS: reolink_entry_ids,
                CONF_CHANNEL: DEFAULT_CHANNEL,
            },
        )
    else:
        reolink_entry_ids = entry.options.get(CONF_REOLINK_ENTRY_IDS) or [
            e.entry_id for e in hass.config_entries.async_entries("reolink")
        ]

    # Let talk_live_view discover cameras/channels from these same Reolink
    # entries (instead of a hardcoded host/channel map that would need
    # per-user source edits and get clobbered on every HACS update).
    hass.data.setdefault(DOMAIN, {})["reolink_entry_ids"] = reolink_entry_ids

    await _async_register_frontend(hass)
    async_register_views(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
