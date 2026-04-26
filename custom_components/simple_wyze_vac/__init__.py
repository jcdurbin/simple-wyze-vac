import logging
import voluptuous as vol
import asyncio

from datetime import timedelta
from functools import lru_cache


import homeassistant.helpers.config_validation as cv

from homeassistant.config_entries import ConfigEntry
from homeassistant import core
from homeassistant.helpers import discovery

from homeassistant.const import (
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_API_KEY,
    CONF_SCAN_INTERVAL
)

from .const import (
    CONF_TOTP,
    CONF_KEY_ID,
    DOMAIN, 
    WYZE_VAC_CLIENT, 
    WYZE_VACUUMS, 
    WYZE_USERNAME, 
    WYZE_PASSWORD, 
    CONF_POLLING, 
    WYZE_SCAN_INTERVAL
)


from wyze_sdk import Client

_LOGGER = logging.getLogger(__name__)

# List of platforms to support. There should be a matching .py file for each,
# eg <cover.py> and <sensor.py>
PLATFORMS: list[str] = ["vacuum", "switch", "camera", "sensor"]


def _log_ssl_diagnostics() -> None:
    """Dump certifi/openssl/truststore state to the HA log.

    Called on every config entry setup so we can diagnose Python 3.14 / certifi
    chain validation failures (HA 2026.3+) without needing host shell access.
    Looks for DigiCert Global Root CA in certifi's bundle since that's the root
    `api.wyzecam.com` chains to.
    """
    import os
    import ssl
    import sys

    _LOGGER.warning("[swv-diag] python=%s", sys.version.replace("\n", " "))
    _LOGGER.warning("[swv-diag] openssl=%s", ssl.OPENSSL_VERSION)

    try:
        import certifi
        bundle_path = certifi.where()
        bundle_exists = os.path.exists(bundle_path)
        bundle_size = os.path.getsize(bundle_path) if bundle_exists else 0
        _LOGGER.warning(
            "[swv-diag] certifi=%s path=%s exists=%s size=%d",
            certifi.__version__, bundle_path, bundle_exists, bundle_size,
        )
        if bundle_exists:
            with open(bundle_path, "rb") as f:
                bundle = f.read()
            for marker in (
                b"DigiCert Global Root CA",
                b"DigiCert Global Root G2",
                b"DigiCert Global Root G3",
            ):
                _LOGGER.warning(
                    "[swv-diag] certifi-contains %r = %s",
                    marker.decode(), marker in bundle,
                )
    except Exception as err:
        _LOGGER.warning("[swv-diag] certifi inspection failed: %s", err)

    try:
        default_paths = ssl.get_default_verify_paths()
        _LOGGER.warning("[swv-diag] ssl-default-paths=%r", default_paths)
        for p in (default_paths.cafile, default_paths.openssl_cafile,
                  "/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem"):
            if p:
                _LOGGER.warning(
                    "[swv-diag] os-bundle %s exists=%s",
                    p, os.path.exists(p),
                )
    except Exception as err:
        _LOGGER.warning("[swv-diag] ssl path inspection failed: %s", err)

    try:
        import truststore
        _LOGGER.warning("[swv-diag] truststore=%s", getattr(truststore, "__version__", "unknown"))
    except Exception as err:
        _LOGGER.warning("[swv-diag] truststore import failed: %s", err)

    try:
        import urllib.request
        urllib.request.urlopen("https://api.wyzecam.com/", timeout=10)
        _LOGGER.warning("[swv-diag] urlopen-default: HTTP success")
    except Exception as err:
        _LOGGER.warning("[swv-diag] urlopen-default: %s: %s", type(err).__name__, err)


@lru_cache(maxsize=1)
def _patch_wyze_sdk_ssl() -> None:
    """Force wyze_sdk's requests sessions to validate via the OS trust store."""
    try:
        import ssl
        import truststore
        from requests.adapters import HTTPAdapter
        from wyze_sdk.service import base as wyze_base
    except Exception as err:  # pragma: no cover
        _LOGGER.warning("Could not patch wyze_sdk SSL handling: %s", err)
        return

    class _TruststoreAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            kwargs["ssl_context"] = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            return super().init_poolmanager(*args, **kwargs)

    original_do_request = wyze_base.BaseServiceClient._do_request

    def _do_request_with_truststore(self, session, request):
        session.mount("https://", _TruststoreAdapter())
        return original_do_request(self, session, request)

    wyze_base.BaseServiceClient._do_request = _do_request_with_truststore


async def async_setup_entry(hass: core.HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Hello World from a config entry."""
    await hass.async_add_executor_job(_log_ssl_diagnostics)
    await hass.async_add_executor_job(_patch_wyze_sdk_ssl)

    # Store an instance of the "connecting" class that does the work of speaking
    # with your actual devices.
    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)
    key_id = entry.data.get(CONF_KEY_ID)
    api_key = entry.data.get(CONF_API_KEY)
    totp = entry.data.get(CONF_TOTP) if entry.data.get(CONF_TOTP) else None

    client = await hass.async_add_executor_job(Client, None, None, username, password, key_id, api_key, totp)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = client

    hass.data[WYZE_VACUUMS] = []

    hass.data[WYZE_USERNAME] = username
    hass.data[WYZE_PASSWORD] = password
    hass.data[CONF_KEY_ID] = key_id
    hass.data[CONF_API_KEY] = api_key
    hass.data[CONF_TOTP] = totp
    hass.data[CONF_POLLING] = entry.options.get(CONF_POLLING)
    hass.data[WYZE_SCAN_INTERVAL] = entry.options.get(CONF_SCAN_INTERVAL)

    device_list = await hass.async_add_executor_job(client.vacuums.list)
    for device in device_list:
        _LOGGER.info(
            "Discovered Wyze device on account: %s with ID %s",
            username,
            device.mac,
        )

        vac_info = await hass.async_add_executor_job(lambda: client.vacuums.info(device_mac=device.mac))

        try:
            rooms = []
            maps = await hass.async_add_executor_job(lambda: client.vacuums.get_maps(device_mac=device.mac))
            room_manager = SWVRoomManager({})
            for map in maps:
                if map.rooms:
                    rooms = rooms + map.rooms
            room_manager = SWVRoomManager(rooms)
                    
        except Exception as err:
            _LOGGER.warn("Failed to query vacuum rooms. If your firmware is higher than 1.6.113, rooms is currently not supported. Exception: " +  str(err))
            room_manager = SWVRoomManager({})

        payload = {
            "mac": device.mac,
            "model": device.product.model,
            "name": device.nickname,
            "suction": vac_info.clean_level.describe(),
            "filter": vac_info.supplies.filter.remaining,
            "main_brush": vac_info.supplies.main_brush.remaining,
            "side_brush": vac_info.supplies.side_brush.remaining,
            "room_manager": room_manager,
            "battery": WyzeBattery(vac_info.voltage)
        }

        hass.data[WYZE_VACUUMS].append(payload)

    # This creates each HA object for each platform your device requires.
    # It's done by calling the `async_setup_entry` function in each platform module.
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(update_listener))
    return True

async def async_unload_entry(hass: core.HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # This is called when an entry/configured device is to be removed. The class
    # needs to unload itself, and remove callbacks. See the classes for further
    # details
    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, component)
                for component in PLATFORMS
            ]
        )
    )
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok

async def update_listener(hass, entry):
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)

class SWVRoomManager:
    def __init__(self, rooms):
        self._rooms = {}
        for room in rooms:
            self._rooms[room.name] = True
    
    @property
    def rooms(self):
        return self._rooms

    def set(self, room_name):
        self._rooms[room_name] = True

    def clear(self, room_name):
        self._rooms[room_name] = False

class WyzeBattery:

    def __init__(self, value=0):
        self.value = value
    