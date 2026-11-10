import logging
from datetime import timedelta

import voluptuous as vol

from homeassistant.core import callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)

from .const import DOMAIN, DATA_ICA
from .ica_api import ICAApi
from .sync import ListSync

_LOGGER = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(minutes=10)   # ICA does not push; this is how ICA-side changes arrive
STARTUP_DELAY = 60                      # seconds; lets the to-do entity come up first
DEBOUNCE_SECONDS = 5                    # after a to-do change
SELF_CHANGE_GRACE = 3                   # ignore the state changes our own sync causes

REFRESH_SCHEMA = vol.Schema({vol.Optional("force", default=False): bool})


async def async_setup(hass, config):
    return True


async def async_setup_entry(hass, entry):
    session_id = entry.options.get("session_id", entry.data["session_id"])
    list_id = entry.options.get("ica_list_id", entry.data["ica_list_id"])
    todo_entity = entry.options.get("todo_entity_id", entry.data.get("todo_entity_id"))
    if not todo_entity:
        _LOGGER.error("No to-do entity selected; the integration cannot sync without one")
        return False

    api = ICAApi(hass, session_id=session_id, entry_id=entry.entry_id)
    hass.data.setdefault(DOMAIN, {})[DATA_ICA] = api
    hass.data[DOMAIN]["current_list_id"] = list_id
    sync = ListSync(hass, entry, api)

    # --- when to run ---
    async def run_timer(_now=None):
        await sync.async_run()

    debounce = None

    @callback
    def on_todo_change(event):
        nonlocal debounce
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if old is None or new is None or old.state == new.state:
            return
        if sync.busy or hass.loop.time() - sync.last_finished < SELF_CHANGE_GRACE:
            return  # caused by our own sync
        if debounce:
            debounce()
        debounce = async_call_later(hass, DEBOUNCE_SECONDS, run_timer)

    @callback
    def cancel_debounce():
        if debounce:
            debounce()

    entry.async_on_unload(async_track_state_change_event(hass, [todo_entity], on_todo_change))
    entry.async_on_unload(async_track_time_interval(hass, run_timer, POLL_INTERVAL))
    entry.async_on_unload(async_call_later(hass, STARTUP_DELAY, run_timer))
    entry.async_on_unload(cancel_debounce)

    async def handle_refresh(call):
        await sync.async_run(force=call.data["force"])

    hass.services.async_register(DOMAIN, "refresh", handle_refresh, schema=REFRESH_SCHEMA)
    entry.async_on_unload(lambda: hass.services.async_remove(DOMAIN, "refresh"))

    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])
    entry.async_on_unload(entry.add_update_listener(_options_update_listener))
    return True


async def async_unload_entry(hass, entry):
    return await hass.config_entries.async_unload_platforms(entry, ["sensor"])


async def _options_update_listener(hass, entry):
    _LOGGER.debug("Options changed, reloading entry")
    hass.data[DOMAIN]["current_list_id"] = entry.options.get(
        "ica_list_id", entry.data.get("ica_list_id")
    )
    await hass.config_entries.async_reload(entry.entry_id)
