"""Bosch Indego Mower integration."""
from typing import Optional
import asyncio
import math
import os
import json
import logging
from datetime import datetime, timedelta
from aiohttp.client_exceptions import ClientResponseError

import homeassistant.util.dt
import voluptuous as vol
from homeassistant.core import HomeAssistant, CoreState
from homeassistant.exceptions import HomeAssistantError, ConfigEntryAuthFailed
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_ICON,
    CONF_ID,
    CONF_NAME,
    CONF_TYPE,
    CONF_UNIT_OF_MEASUREMENT,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
    STATE_ON,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_call_later
from homeassistant.util.dt import utcnow
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import async_get_config_entry_implementation
from homeassistant.helpers.event import async_track_point_in_time
from pyIndego import IndegoAsyncClient

from .api import IndegoOAuth2Session
from .binary_sensor import IndegoBinarySensor
from .vacuum import IndegoVacuum
from .lawn_mower import IndegoLawnMower
from .const import *
from .sensor import IndegoSensor

_LOGGER = logging.getLogger(__name__)

SERVICE_SCHEMA_COMMAND = vol.Schema({
    vol.Optional(CONF_MOWER_SERIAL): cv.string,
    vol.Required(CONF_SEND_COMMAND): cv.string
})

SERVICE_SCHEMA_SMARTMOWING = vol.Schema({
    vol.Optional(CONF_MOWER_SERIAL): cv.string,
    vol.Required(CONF_SMARTMOWING): cv.string
})

SERVICE_SCHEMA_DELETE_ALERT = vol.Schema({
    vol.Optional(CONF_MOWER_SERIAL): cv.string,
    vol.Required(SERVER_DATA_ALERT_INDEX): cv.positive_int
})

SERVICE_SCHEMA_DELETE_ALERT_ALL = vol.Schema({
    vol.Optional(CONF_MOWER_SERIAL): cv.string
})

SERVICE_SCHEMA_READ_ALERT = vol.Schema({
    vol.Optional(CONF_MOWER_SERIAL): cv.string,
    vol.Required(SERVER_DATA_ALERT_INDEX): cv.positive_int
})

SERVICE_SCHEMA_READ_ALERT_ALL = vol.Schema({
    vol.Optional(CONF_MOWER_SERIAL): cv.string
})


def FUNC_ICON_MOWER_ALERT(state):
    if state:
        if int(state) > 0 or state == STATE_ON:
            return "mdi:alert-outline"
    return "mdi:check-circle-outline"


ENTITY_DEFINITIONS = {
    ENTITY_ONLINE: {
        CONF_TYPE: BINARY_SENSOR_TYPE,
        CONF_NAME: "online",
        CONF_ICON: "mdi:cloud-check",
        CONF_DEVICE_CLASS: BinarySensorDeviceClass.CONNECTIVITY,
        CONF_ATTR: [],
    },
    ENTITY_UPDATE_AVAILABLE: {
        CONF_TYPE: BINARY_SENSOR_TYPE,
        CONF_NAME: "update available",
        CONF_ICON: "mdi:download-outline",
        CONF_DEVICE_CLASS: BinarySensorDeviceClass.UPDATE,
        CONF_ATTR: [],
    },
    ENTITY_ALERT: {
        CONF_TYPE: BINARY_SENSOR_TYPE,
        CONF_NAME: "alert",
        CONF_ICON: FUNC_ICON_MOWER_ALERT,
        CONF_DEVICE_CLASS: BinarySensorDeviceClass.PROBLEM,
        CONF_ATTR: ["alerts_count"],
        CONF_TRANSLATION_KEY: "indego_alert",
    },
    ENTITY_MOWER_STATE: {
        CONF_TYPE: SENSOR_TYPE,
        CONF_NAME: "mower state",
        CONF_ICON: "mdi:robot-mower-outline",
        CONF_DEVICE_CLASS: None,
        CONF_UNIT_OF_MEASUREMENT: None,
        CONF_ATTR: ["last_updated"],
        CONF_TRANSLATION_KEY: "mower_state",
    },
    ENTITY_MOWER_STATE_DETAIL: {
        CONF_TYPE: SENSOR_TYPE,
        CONF_NAME: "mower state detail",
        CONF_ICON: "mdi:robot-mower-outline",
        CONF_DEVICE_CLASS: None,
        CONF_UNIT_OF_MEASUREMENT: None,
        CONF_ATTR: [
            "last_updated",
            "state_number",
            "state_description",
        ],
        CONF_TRANSLATION_KEY: "mower_state_detail",
    },
    ENTITY_BATTERY: {
        CONF_TYPE: SENSOR_TYPE,
        CONF_NAME: "battery %",
        CONF_ICON: "battery",
        CONF_DEVICE_CLASS: SensorDeviceClass.BATTERY,
        CONF_UNIT_OF_MEASUREMENT: "%",
        CONF_ATTR: [
            "last_updated",
            "voltage_V",
            "discharge_Ah",
            "cycles",
            f"battery_temp_{UnitOfTemperature.CELSIUS}",
            f"ambient_temp_{UnitOfTemperature.CELSIUS}",
        ],
    },
    ENTITY_LAWN_MOWED: {
        CONF_TYPE: SENSOR_TYPE,
        CONF_NAME: "lawn mowed",
        CONF_ICON: "mdi:grass",
        CONF_DEVICE_CLASS: None,
        CONF_UNIT_OF_MEASUREMENT: "%",
        CONF_ATTR: [
            "last_updated",
            "last_completed_mow",
            "next_mow",
            "last_session_operation_min",
            "last_session_cut_min",
            "last_session_charge_min",
        ],
    },
    ENTITY_LAST_COMPLETED: {
        CONF_TYPE: SENSOR_TYPE,
        CONF_NAME: "last completed",
        CONF_ICON: "mdi:calendar-check",
        CONF_DEVICE_CLASS: SensorDeviceClass.TIMESTAMP,
        CONF_UNIT_OF_MEASUREMENT: None,
        CONF_ATTR: [],
    },
    ENTITY_NEXT_MOW: {
        CONF_TYPE: SENSOR_TYPE,
        CONF_NAME: "next mow",
        CONF_ICON: "mdi:calendar-clock",
        CONF_DEVICE_CLASS: SensorDeviceClass.TIMESTAMP,
        CONF_UNIT_OF_MEASUREMENT: None,
        CONF_ATTR: [],
    },
    ENTITY_MOWING_MODE: {
        CONF_TYPE: SENSOR_TYPE,
        CONF_NAME: "mowing mode",
        CONF_ICON: "mdi:alpha-m-circle-outline",
        CONF_DEVICE_CLASS: None,
        CONF_UNIT_OF_MEASUREMENT: None,
        CONF_ATTR: [],
    },
    ENTITY_RUNTIME: {
        CONF_TYPE: SENSOR_TYPE,
        CONF_NAME: "mowtime total",
        CONF_ICON: "mdi:information-outline",
        CONF_DEVICE_CLASS: None,
        CONF_UNIT_OF_MEASUREMENT: "h",
        CONF_ATTR: [
            "total_mowing_time_h",
            "total_charging_time_h",
            "total_operation_time_h",
        ],
    },
    ENTITY_VACUUM: {
        CONF_TYPE: VACUUM_TYPE,
    },
    ENTITY_LAWN_MOWER: {
        CONF_TYPE: LAWN_MOWER_TYPE,
    },
    ENTITY_MOWER_SVG_X: {
        CONF_TYPE: SENSOR_TYPE,
        CONF_NAME: "mower position x",
        CONF_ICON: "mdi:map-marker",
        CONF_DEVICE_CLASS: None,
        CONF_UNIT_OF_MEASUREMENT: "px",
        CONF_ATTR: [],
    },
    ENTITY_MOWER_SVG_Y: {
        CONF_TYPE: SENSOR_TYPE,
        CONF_NAME: "mower position y",
        CONF_ICON: "mdi:map-marker",
        CONF_DEVICE_CLASS: None,
        CONF_UNIT_OF_MEASUREMENT: "px",
        CONF_ATTR: [],
    },
    ENTITY_MOWER_STUCK: {
        CONF_TYPE: BINARY_SENSOR_TYPE,
        CONF_NAME: "mower stuck",
        CONF_ICON: "mdi:alert-circle-outline",
        CONF_DEVICE_CLASS: BinarySensorDeviceClass.PROBLEM,
        CONF_ATTR: ["stuck_since", "stuck_x", "stuck_y"],
    },
}


def format_indego_date(date: datetime) -> str:
    return date.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def last_updated_now() -> str:
    return homeassistant.util.dt.as_local(utcnow()).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load a config entry."""
    hass.data.setdefault(DOMAIN, {})

    entry_implementation = await async_get_config_entry_implementation(hass, entry)
    oauth_session = IndegoOAuth2Session(hass, entry, entry_implementation)
    indego_hub = hass.data[DOMAIN][entry.entry_id] = IndegoHub(
        entry.data[CONF_MOWER_NAME],
        oauth_session,
        entry.data[CONF_MOWER_SERIAL],
        {
            CONF_EXPOSE_INDEGO_AS_MOWER: entry.options.get(CONF_EXPOSE_INDEGO_AS_MOWER, False),
            CONF_EXPOSE_INDEGO_AS_VACUUM: entry.options.get(CONF_EXPOSE_INDEGO_AS_VACUUM, False),
            CONF_SHOW_ALL_ALERTS: entry.options.get(CONF_SHOW_ALL_ALERTS, False),
        },
        hass,
        entry.options.get(CONF_USER_AGENT)
    )

    async def load_platforms():
        _LOGGER.debug("Loading platforms")
        await hass.config_entries.async_forward_entry_setups(entry, INDEGO_PLATFORMS)

    try:
        await indego_hub.update_generic_data_and_load_platforms(load_platforms)

    except ClientResponseError as exc:
        if 400 <= exc.status < 500:
            _LOGGER.debug("Received 401, triggering ConfigEntryAuthFailed in HA...")
            raise ConfigEntryAuthFailed from exc

        _LOGGER.warning("Login unsuccessful: %s", str(exc))
        return False

    except AttributeError as exc:
        _LOGGER.warning("Login unsuccessful: %s", str(exc))
        return False

    def find_instance_for_mower_service_call(call):
        mower_serial = call.data.get(CONF_MOWER_SERIAL, None)
        if mower_serial is None:
            # Return the first instance when params is missing for backwards compatibility.
            return hass.data[DOMAIN][hass.data[DOMAIN][CONF_SERVICES_REGISTERED]]

        for config_entry_id in hass.data[DOMAIN]:
            if config_entry_id == CONF_SERVICES_REGISTERED:
                continue

            instance = hass.data[DOMAIN][config_entry_id]
            if instance.serial == mower_serial:
                return instance

        raise HomeAssistantError("No mower instance found for serial '%s'" % mower_serial)

    async def async_send_command(call):
        """Handle the mower command service call."""
        instance = find_instance_for_mower_service_call(call)
        command = call.data.get(CONF_SEND_COMMAND, DEFAULT_NAME_COMMANDS)
        _LOGGER.debug("Indego.send_command service called, with command: %s", command)

        await instance.async_send_command_to_client(command)

    async def async_send_smartmowing(call):
        """Handle the smartmowing service call."""
        instance = find_instance_for_mower_service_call(call)
        enable = call.data.get(CONF_SMARTMOWING, DEFAULT_NAME_COMMANDS)
        _LOGGER.debug("Indego.send_smartmowing service called, enable: %s", enable)

        await instance._indego_client.put_mow_mode(enable)
        await instance._update_generic_data()

    async def async_delete_alert(call):
        """Handle the service call."""
        instance = find_instance_for_mower_service_call(call)
        index = call.data.get(SERVER_DATA_ALERT_INDEX, DEFAULT_NAME_COMMANDS)
        _LOGGER.debug("Indego.delete_alert service called with alert index: %s", index)

        await instance._update_alerts()
        await instance._indego_client.delete_alert(index)
        await instance._update_alerts()     

    async def async_delete_alert_all(call):
        """Handle the service call."""
        instance = find_instance_for_mower_service_call(call)
        _LOGGER.debug("Indego.delete_alert_all service called")

        await instance._update_alerts()
        await instance._indego_client.delete_all_alerts()
        await instance._update_alerts()   

    async def async_read_alert(call):
        """Handle the service call."""
        instance = find_instance_for_mower_service_call(call)
        index = call.data.get(SERVER_DATA_ALERT_INDEX, DEFAULT_NAME_COMMANDS)
        _LOGGER.debug("Indego.read_alert service called with alert index: %s", index)

        await instance._update_alerts()
        await instance._indego_client.put_alert_read(index)
        await instance._update_alerts()

    async def async_read_alert_all(call):
        """Handle the service call."""
        instance = find_instance_for_mower_service_call(call)
        _LOGGER.debug("Indego.read_alert_all service called")

        await instance._update_alerts()
        await instance._indego_client.put_all_alerts_read()
        await instance._update_alerts()

    # In HASS we can have multiple Indego component instances as long as the mower serial is unique.
    # So the mower services should only need to be registered for the first instance.
    if CONF_SERVICES_REGISTERED not in hass.data[DOMAIN]:
        _LOGGER.debug("Initializing mower service for config entry '%s'", entry.entry_id)

        hass.services.async_register(
            DOMAIN,
            SERVICE_NAME_COMMAND,
            async_send_command,
            schema=SERVICE_SCHEMA_COMMAND
        )

        hass.services.async_register(
            DOMAIN,
            SERVICE_NAME_SMARTMOW,
            async_send_smartmowing,
            schema=SERVICE_SCHEMA_SMARTMOWING,
        )
        hass.services.async_register(
            DOMAIN, 
            SERVICE_NAME_DELETE_ALERT, 
            async_delete_alert, 
            schema=SERVICE_SCHEMA_DELETE_ALERT
        )
        hass.services.async_register(
            DOMAIN, 
            SERVICE_NAME_READ_ALERT, 
            async_read_alert, 
            schema=SERVICE_SCHEMA_READ_ALERT
        )
        hass.services.async_register(
            DOMAIN, 
            SERVICE_NAME_DELETE_ALERT_ALL, 
            async_delete_alert_all, 
            schema=SERVICE_SCHEMA_DELETE_ALERT_ALL
        )
        hass.services.async_register(
            DOMAIN, 
            SERVICE_NAME_READ_ALERT_ALL, 
            async_read_alert_all, 
            schema=SERVICE_SCHEMA_READ_ALERT_ALL
        )

        hass.data[DOMAIN][CONF_SERVICES_REGISTERED] = entry.entry_id

    else:
        _LOGGER.debug("Indego mower services already registered. Skipping for config entry '%s'", entry.entry_id)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""

    unload_ok = await hass.config_entries.async_unload_platforms(entry, INDEGO_PLATFORMS)
    if not unload_ok:
        return False

    if CONF_SERVICES_REGISTERED in hass.data[DOMAIN] and hass.data[DOMAIN][CONF_SERVICES_REGISTERED] == entry.entry_id:
        del hass.data[DOMAIN][CONF_SERVICES_REGISTERED]

    await hass.data[DOMAIN][entry.entry_id].async_shutdown()
    del hass.data[DOMAIN][entry.entry_id]

    return True


class IndegoHub:
    """Class for the IndegoHub, which controls the sensors and binary sensors."""

    def __init__(self, name: str, session: IndegoOAuth2Session, serial: str, features: dict, hass: HomeAssistant, user_agent: Optional[str] = None):
        """Initialize the IndegoHub.

        Args:
            name (str): the name of the mower for entities
            session (IndegoOAuth2Session): the Bosch SingleKey ID OAuth session
            serial (str): serial of the mower, is used for uniqueness
            hass (HomeAssistant): HomeAssistant instance

        """
        self._mower_name = name
        self._serial = serial
        self._features = features
        self._hass = hass
        self._unsub_refresh_state = None
        self._refresh_state_task = None
        self._refresh_10m_remover = None
        self._refresh_24h_remover = None
        self._fast_poll_task = None
        self._shutdown = False
        self._latest_alert = None
        self.entities = {}
        self._update_fail_count = None
        self._last_position_change_time = None
        self._last_svg_x = None
        self._last_svg_y = None
        self._map_svg = None
        self._sessions = self._load_trail()
        # Restore last session as current so it stays bright green after restart
        self._current_session = self._sessions[-1] if self._sessions else None
        self._was_mowing = False
        self._stuck_positions = self._load_stuck()
        self._was_stuck = False
        self._map_svg = self._load_base_map()
        # Regenerate annotated map on startup from last known position
        if self._sessions:
            last_session = self._sessions[-1]
            if last_session and last_session.get('points'):
                last_x, last_y = last_session['points'][-1]
                self._hass.loop.call_soon(
                    lambda: self._hass.async_create_task(
                        self._update_map_svg(last_x, last_y)
                    )
                )

        async def async_token_refresh() -> str:
            await session.async_ensure_token_valid()
            return session.token["access_token"]

        self._indego_client = IndegoAsyncClient(
            token=session.token["access_token"],
            token_refresh_method=async_token_refresh,
            serial=self._serial,
            session=async_get_clientsession(hass),
            raise_request_exceptions=True
        )
        self._indego_client.set_default_header(HTTP_HEADER_USER_AGENT, user_agent)

    async def async_send_command_to_client(self, command: str):
        """Send a mower command to the Indego client."""
        _LOGGER.debug("Sending command to mower (%s): '%s'", self._serial, command)
        await self._indego_client.put_command(command)

    def _create_entities(self, device_info):
        """Create sub-entities and add them to Hass."""

        _LOGGER.debug("Creating entities")

        for entity_key, entity in ENTITY_DEFINITIONS.items():
            if entity[CONF_TYPE] == SENSOR_TYPE:
                self.entities[entity_key] = IndegoSensor(
                    f"indego_{self._serial}_{entity_key}",
                    f"{self._mower_name} {entity[CONF_NAME]}",
                    entity[CONF_ICON],
                    entity[CONF_DEVICE_CLASS],
                    entity[CONF_UNIT_OF_MEASUREMENT],
                    entity[CONF_ATTR],
                    device_info,
                    translation_key=entity[CONF_TRANSLATION_KEY] if CONF_TRANSLATION_KEY in entity else None,
                )

            elif entity[CONF_TYPE] == BINARY_SENSOR_TYPE:
                self.entities[entity_key] = IndegoBinarySensor(
                    f"indego_{self._serial}_{entity_key}",
                    f"{self._mower_name} {entity[CONF_NAME]}",
                    entity[CONF_ICON],
                    entity[CONF_DEVICE_CLASS],
                    entity[CONF_ATTR],
                    device_info,
                    translation_key=entity[CONF_TRANSLATION_KEY] if CONF_TRANSLATION_KEY in entity else None,
                )

            elif entity[CONF_TYPE] == LAWN_MOWER_TYPE:
                if self._features[CONF_EXPOSE_INDEGO_AS_MOWER]:
                    self.entities[entity_key] = IndegoLawnMower(
                        f"indego_{self._serial}",
                        self._mower_name,
                        device_info,
                        self
                    )

            elif entity[CONF_TYPE] == VACUUM_TYPE:
                if self._features[CONF_EXPOSE_INDEGO_AS_VACUUM]:
                    self.entities[entity_key] = IndegoVacuum(
                        f"indego_{self._serial}",
                        self._mower_name,
                        device_info,
                        self
                    )

    async def update_generic_data_and_load_platforms(self, load_platforms):
        """Update the generic mower data, so we can create the HA platforms for the Indego component."""
        _LOGGER.debug("Getting generic data for device info.")
        generic_data = await self._update_generic_data()

        device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            manufacturer="Bosch",
            name=self._mower_name,
            model=generic_data.bareToolnumber if generic_data else None,
            sw_version=generic_data.alm_firmware_version if generic_data else None,
        )

        self._create_entities(device_info)
        await load_platforms()

        if self._hass.state == CoreState.running:
            # HA has already been started (this probably an integration reload).
            # Perform initial update right away...
            self._hass.async_create_task(self._initial_update())

        else:
            # HA is still starting, delay the initial update...
            self._hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._initial_update
            )

        self._hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.async_shutdown)

    async def _initial_update(self, _=None):
        """Do the initial update and create all entities."""
        _LOGGER.debug("Starting initial update.")

        self.set_online_state(False)
        await self._create_refresh_state_task()
        await asyncio.gather(*[self.refresh_10m(), self.refresh_24h()])

        try:
            _LOGGER.debug("Refreshing initial operating data.")
            await self._update_operating_data()

        except Exception as exc:
            _LOGGER.warning("Error %s for while performing initial update", str(exc))

    async def async_shutdown(self, _=None):
        """Remove all future updates, cancel tasks and close the client."""
        if self._shutdown:
            return

        _LOGGER.debug("Starting shutdown.")
        self._shutdown = True

        self._cancel_delayed_refresh_state()

        if self._refresh_state_task:
            self._refresh_state_task.cancel()
            await self._refresh_state_task
            self._refresh_state_task = None

        if self._fast_poll_task:
            self._fast_poll_task.cancel()
            self._fast_poll_task = None

        if self._refresh_10m_remover:
            self._refresh_10m_remover()

        if self._refresh_24h_remover:
            self._refresh_24h_remover()

        await self._indego_client.close()
        _LOGGER.debug("Shutdown finished.")

    async def refresh_state(self):
        """Update the state, if necessary update operating data and recall itself."""
        _LOGGER.debug("Refreshing state.")
        self._cancel_delayed_refresh_state()

        update_failed = False
        try:
            await self._update_state(longpoll=(self._update_fail_count is None or self._update_fail_count == 0))
            self._update_fail_count = 0

        except Exception as exc:
            update_failed = True
            _LOGGER.warning("Mower state update failed, reason: %s", str(exc))
            self.set_online_state(False)

        if self._shutdown:
            return

        if update_failed:
            if self._update_fail_count is None:
                self._update_fail_count = 1
            _LOGGER.debug("Delaying next status update with %i seconds due to previous failure...", STATUS_UPDATE_FAILURE_DELAY_TIME[self._update_fail_count])
            when = datetime.now() + timedelta(seconds=STATUS_UPDATE_FAILURE_DELAY_TIME[self._update_fail_count])
            self._update_fail_count = min(self._update_fail_count + 1, len(STATUS_UPDATE_FAILURE_DELAY_TIME) - 1)
            self._unsub_refresh_state = async_track_point_in_time(self._hass, self._create_refresh_state_task, when)
            return

        if self._indego_client.state:
            state = self._indego_client.state.state
            if (500 <= state <= 799) or (state in (257, 260)):
                try:
                    _LOGGER.debug("Refreshing operating data.")
                    await self._update_operating_data()

                except Exception as exc:
                    _LOGGER.warning("Mower operating data update failed, reason: %s", str(exc))

            if self._indego_client.state.error != self._latest_alert:
                self._latest_alert = self._indego_client.state.error
                try:
                    _LOGGER.debug("Refreshing alerts, to get new alert.")
                    await self._update_alerts()

                except Exception as exc:
                    _LOGGER.warning("Mower alerts update failed, reason: %s", str(exc))

        await self._create_refresh_state_task()

    async def _fast_poll_loop(self):
        """Poll position every 3 seconds while mowing, without longpoll."""
        import asyncio
        _LOGGER.debug("Indego: fast poll loop started")
        try:
            while True:
                await asyncio.sleep(3)
                if self._shutdown:
                    break
                try:
                    await self._indego_client.update_state(longpoll=False)
                    if self._indego_client.state:
                        svg_x = self._indego_client.state.svg_xPos
                        svg_y = self._indego_client.state.svg_yPos
                        if svg_x is not None and svg_y is not None:
                            if ENTITY_MOWER_SVG_X in self.entities:
                                self.entities[ENTITY_MOWER_SVG_X].state = svg_x
                            if ENTITY_MOWER_SVG_Y in self.entities:
                                self.entities[ENTITY_MOWER_SVG_Y].state = svg_y
                            is_mowing = 500 <= self._indego_client.state.state <= 799
                            if not is_mowing:
                                _LOGGER.debug("Indego: fast poll loop stopping, no longer mowing")
                                break
                            # Append to trail
                            if self._current_session is not None:
                                pts = self._current_session.get('points', [])
                                last = pts[-1] if pts else None
                                if last is None or abs(svg_x - last[0]) > 2 or abs(svg_y - last[1]) > 2:
                                    self._current_session['points'].append([svg_x, svg_y])
                                    if not self._sessions or self._sessions[-1] is not self._current_session:
                                        self._sessions.append(self._current_session)
                                    self._sessions = self._sessions[-4:]
                                    self._save_trail()
                            self._hass.async_create_task(self._update_map_svg(svg_x, svg_y))
                except Exception as exc:
                    _LOGGER.debug("Indego fast poll error: %s", str(exc))
        except Exception:
            pass
        _LOGGER.debug("Indego: fast poll loop ended")

    async def _create_refresh_state_task(self, event=None):
        """Create a task to refresh the mower state."""
        self._refresh_state_task = self._hass.async_create_task(self.refresh_state())

    def _cancel_delayed_refresh_state(self):
        """Cancel a delayed refresh state callback (if any exists)."""
        if self._unsub_refresh_state is None:
            return

        self._unsub_refresh_state()
        self._unsub_refresh_state = None

    async def refresh_10m(self, _=None):
        """Refresh Indego sensors every 10m."""
        _LOGGER.debug("Refreshing 10m.")

        results = await asyncio.gather(
            *[
                self._update_generic_data(),
                self._update_alerts(),
                self._update_last_completed_mow(),
                self._update_next_mow(),
            ],
            return_exceptions=True,
        )

        next_refresh = 600
        index = 0
        for res in results:
            if res and isinstance(res, BaseException):
                try:
                    raise res
                except Exception as exc:
                    _LOGGER.warning("Error %s for index %i while performing 10m update", str(exc), index)
            index += 1

        self._refresh_10m_remover = async_call_later(
            self._hass, next_refresh, self.refresh_10m
        )

    async def refresh_24h(self, _=None):
        """Refresh Indego sensors every 24h."""
        _LOGGER.debug("Refreshing 24h.")

        try:
            await self._update_updates_available()

        except Exception as exc:
            _LOGGER.warning("Error %s while performing 24h update", str(exc))

        self._refresh_24h_remover = async_call_later(self._hass, 86400, self.refresh_24h)

    async def _update_operating_data(self):
        await self._indego_client.update_operating_data()

        _LOGGER.debug(f"Updating operating data")
        if self._indego_client.operating_data:
            self.entities[ENTITY_BATTERY].state = self._indego_client.operating_data.battery.percent_adjusted

            if ENTITY_VACUUM in self.entities:
                self.entities[ENTITY_VACUUM].battery_level = self._indego_client.operating_data.battery.percent_adjusted

            self.entities[ENTITY_BATTERY].add_attributes(
                {
                    "last_updated": last_updated_now(),
                    "voltage_V": self._indego_client.operating_data.battery.voltage,
                    "discharge_Ah": self._indego_client.operating_data.battery.discharge,
                    "cycles": self._indego_client.operating_data.battery.cycles,
                    f"battery_temp_{UnitOfTemperature.CELSIUS}": self._indego_client.operating_data.battery.battery_temp,
                    f"ambient_temp_{UnitOfTemperature.CELSIUS}": self._indego_client.operating_data.battery.ambient_temp,
                }
            )

    def set_online_state(self, online: bool):
        _LOGGER.debug("Set online state: %s", online)

        self.entities[ENTITY_ONLINE].state = online
        self.entities[ENTITY_MOWER_STATE].set_cloud_connection_state(online)
        self.entities[ENTITY_MOWER_STATE_DETAIL].set_cloud_connection_state(online)

        if ENTITY_VACUUM in self.entities:
            self.entities[ENTITY_VACUUM].set_cloud_connection_state(online)

        if ENTITY_LAWN_MOWER in self.entities:
            self.entities[ENTITY_LAWN_MOWER].set_cloud_connection_state(online)

    async def _update_state(self, longpoll: bool = True):
        await self._indego_client.update_state(longpoll=longpoll, longpoll_timeout=230)

        if self._shutdown:
            return

        if not self._indego_client.state:
            self.set_online_state(False)
            return  # State update failed

        self.set_online_state(self._indego_client.online)
        is_mowing_state = 500 <= self._indego_client.state.state <= 799
        if is_mowing_state and (self._fast_poll_task is None or self._fast_poll_task.done()):
            self._fast_poll_task = self._hass.async_create_task(self._fast_poll_loop())
        elif not is_mowing_state and self._fast_poll_task and not self._fast_poll_task.done():
            self._fast_poll_task.cancel()
            self._fast_poll_task = None
        self.entities[ENTITY_MOWER_STATE].state = self._indego_client.state_description
        self.entities[ENTITY_MOWER_STATE_DETAIL].state = self._indego_client.state_description_detail
        self.entities[ENTITY_LAWN_MOWED].state = self._indego_client.state.mowed
        self.entities[ENTITY_RUNTIME].state = self._indego_client.state.runtime.total.cut
        self.entities[ENTITY_BATTERY].charging = (
            True if self._indego_client.state_description_detail == "Charging" else False
        )

        self.entities[ENTITY_MOWER_STATE].add_attributes(
            {
                "last_updated": last_updated_now()
            }
        )

        self.entities[ENTITY_MOWER_STATE_DETAIL].add_attributes(
            {
                "last_updated": last_updated_now(),
                "state_number": self._indego_client.state.state,
                "state_description": self._indego_client.state_description_detail,
            }
        )

        self.entities[ENTITY_LAWN_MOWED].add_attributes(
            {
                "last_updated": last_updated_now(),
                "last_session_operation_min": self._indego_client.state.runtime.session.operate,
                "last_session_cut_min": self._indego_client.state.runtime.session.cut,
                "last_session_charge_min": self._indego_client.state.runtime.session.charge,
            }
        )

        self.entities[ENTITY_RUNTIME].add_attributes(
            {
                "total_operation_time_h": self._indego_client.state.runtime.total.operate,
                "total_mowing_time_h": self._indego_client.state.runtime.total.cut,
                "total_charging_time_h": self._indego_client.state.runtime.total.charge,
            }
        )

        if ENTITY_VACUUM in self.entities:
            self.entities[ENTITY_VACUUM].indego_state = self._indego_client.state.state
            self.entities[ENTITY_VACUUM].battery_charging = self.entities[ENTITY_BATTERY].charging

        if ENTITY_LAWN_MOWER in self.entities:
            self.entities[ENTITY_LAWN_MOWER].indego_state = self._indego_client.state.state
            # Position tracking and stuck detection
        
        # Position tracking and stuck detection
        svg_x = self._indego_client.state.svg_xPos
        svg_y = self._indego_client.state.svg_yPos

        if svg_x is not None and svg_y is not None:
            if ENTITY_MOWER_SVG_X in self.entities:
                self.entities[ENTITY_MOWER_SVG_X].state = svg_x
            if ENTITY_MOWER_SVG_Y in self.entities:
                self.entities[ENTITY_MOWER_SVG_Y].state = svg_y

            is_mowing = 500 <= self._indego_client.state.state <= 799
            now = datetime.now()

            moved = self._last_svg_x is None or math.sqrt(
                (svg_x - self._last_svg_x) ** 2 + (svg_y - self._last_svg_y) ** 2
            ) > 5

            if moved:
                self._last_svg_x = svg_x
                self._last_svg_y = svg_y
                self._last_position_change_time = now

            stuck = (
                is_mowing
                and self._last_position_change_time is not None
                and (now - self._last_position_change_time).total_seconds() > 60
            )

            if ENTITY_MOWER_STUCK in self.entities:
                self.entities[ENTITY_MOWER_STUCK].state = stuck
                if stuck:
                    self.entities[ENTITY_MOWER_STUCK].add_attributes({
                        "stuck_since": self._last_position_change_time.strftime("%Y-%m-%d %H:%M:%S"),
                        "stuck_x": svg_x,
                        "stuck_y": svg_y,
                    })
                    if not self._was_stuck and self._current_session and len(self._current_session['points']) > 40:
                        self._current_session['stuck'].append([svg_x, svg_y])
                        if len(self._current_session['stuck']) > 10:
                            self._current_session['stuck'] = self._current_session['stuck'][-10:]
                        self._save_trail()
            self._was_stuck = stuck

            if is_mowing:
                if not self._was_mowing:
                    cur_points = len(self._current_session['points']) if self._current_session else 0
                    if self._current_session and cur_points > 5:
                        # Genuinely new mow - start new session
                        from datetime import datetime as _dt
                        next_id = (self._sessions[-1]['id'] + 1) if self._sessions else 1
                        self._current_session = {"id": next_id, "started": _dt.now().isoformat(), "points": [], "stuck": []}
                        _LOGGER.debug('Indego: new mowing session %d started', next_id)
                    elif not self._current_session:
                        from datetime import datetime as _dt
                        self._current_session = {"id": 1, "started": _dt.now().isoformat(), "points": [], "stuck": []}
                        _LOGGER.debug('Indego: starting fresh mowing session')
                    else:
                        _LOGGER.debug('Indego: resuming mowing session %d with %d points', self._current_session['id'], cur_points)
                    # Check if garden map has been updated
                    try:
                        if self._indego_client.state.map_update_available:
                            _LOGGER.debug('Indego: map update available, refreshing base map')
                            www_path = self._hass.config.path("www")
                            svg_path = os.path.join(www_path, f"indego_base_{self._serial}.svg")
                            if os.path.exists(svg_path):
                                os.remove(svg_path)
                            self._map_svg = None
                    except Exception:
                        pass
                self._current_session['points'].append([svg_x, svg_y])
                if not self._sessions or self._sessions[-1] is not self._current_session:
                    self._sessions.append(self._current_session)
                self._sessions = self._sessions[-4:]
                self._save_trail()
            elif self._was_mowing and not is_mowing:
                async def _delayed_map_refresh():
                    import asyncio as _asyncio
                    await _asyncio.sleep(120)
                    try:
                        www_path = self._hass.config.path('www')
                        svg_path = os.path.join(www_path, f'indego_base_{self._serial}.svg')
                        _LOGGER.debug('Indego: refreshing base map after mow completed')
                        await self._indego_client.download_map(filename=svg_path)
                        with open(svg_path, 'r') as mf:
                            self._map_svg = mf.read()
                        last_x = self._last_svg_x or svg_x
                        last_y = self._last_svg_y or svg_y
                        await self._update_map_svg(last_x, last_y)
                    except Exception as exc:
                        _LOGGER.debug('Indego: post-mow map refresh failed: %s', exc)
                self._hass.async_create_task(_delayed_map_refresh())
            self._was_mowing = is_mowing
            self._hass.async_create_task(self._update_map_svg(svg_x, svg_y))

    async def _update_generic_data(self):
        await self._indego_client.update_generic_data()

        if self._indego_client.generic_data:
            if ENTITY_MOWING_MODE in self.entities:
                self.entities[
                    ENTITY_MOWING_MODE
                ].state = self._indego_client.generic_data.mowing_mode_description

        return self._indego_client.generic_data

    async def _update_alerts(self):
        await self._indego_client.update_alerts()

        self.entities[ENTITY_ALERT].state = self._indego_client.alerts_count > 0

        if self._indego_client.alerts:
            self.entities[ENTITY_ALERT].add_attributes(
                {
                    "alerts_count": self._indego_client.alerts_count,
                    "last_alert_error_code": self._indego_client.alerts[0].error_code,
                    "last_alert_message": self._indego_client.alerts[0].message,
                    "last_alert_date": format_indego_date(self._indego_client.alerts[0].date),
                    "last_alert_read": self._indego_client.alerts[0].read_status,
                }, False
            )

            # It's not recommended to track full alerts, disabled by default.
            # See the developer docs: https://developers.home-assistant.io/docs/core/entity/
            if self._features[CONF_SHOW_ALL_ALERTS]:
                alert_index = 0
                for index, alert in enumerate(self._indego_client.alerts):
                    self.entities[ENTITY_ALERT].add_attributes({
                        ("alert_%i" % index): "%s: %s" % (format_indego_date(alert.date), alert.message)
                    }, False)
                    alert_index = index

                # Clear any other alerts that no longer exist.
                alert_index += 1
                while self.entities[ENTITY_ALERT].clear_attribute("alert_%i" % alert_index, False):
                    alert_index += 1

            self.entities[ENTITY_ALERT].async_schedule_update_ha_state()

        else:
            self.entities[ENTITY_ALERT].set_attributes(
                {
                    "alerts_count": self._indego_client.alerts_count
                }
            )

    async def _update_updates_available(self):
        await self._indego_client.update_updates_available()

        self.entities[ENTITY_UPDATE_AVAILABLE].state = self._indego_client.update_available

    async def _update_last_completed_mow(self):
        await self._indego_client.update_last_completed_mow()

        if self._indego_client.last_completed_mow:
            self.entities[
                ENTITY_LAST_COMPLETED
            ].state = self._indego_client.last_completed_mow.isoformat()

            self.entities[ENTITY_LAWN_MOWED].add_attributes(
                {
                    "last_completed_mow": format_indego_date(self._indego_client.last_completed_mow)
                }
            )

    async def _update_next_mow(self):
        await self._indego_client.update_next_mow()

        if self._indego_client.next_mow:
            self.entities[ENTITY_NEXT_MOW].state = self._indego_client.next_mow.isoformat()

            next_mow = format_indego_date(self._indego_client.next_mow)

            self.entities[ENTITY_NEXT_MOW].add_attributes(
                {"next_mow": next_mow}
            )

            self.entities[ENTITY_LAWN_MOWED].add_attributes(
                {"next_mow": next_mow}
            )

    @property
    def serial(self) -> str:
        return self._serial

    @property
    def client(self) -> IndegoAsyncClient:
        return self._indego_client
        
    def _load_base_map(self):
        """Load base SVG map from disk if available."""
        try:
            www_path = self._hass.config.path("www")
            svg_path = os.path.join(www_path, f"indego_base_{self._serial}.svg")
            if os.path.exists(svg_path):
                with open(svg_path, "r") as f:
                    data = f.read()
                    _LOGGER.debug("Loaded base map from disk, len=%s", len(data))
                    return data
        except Exception as exc:
            _LOGGER.warning("Failed to load base map: %s", exc)
        return None

    def _load_trail(self) -> list:
        """Load persisted sessions from disk."""
        try:
            path = self._hass.config.path(f"indego_trail_{self._serial}.json")
            if os.path.exists(path):
                with open(path, "r") as f:
                    data = json.load(f)
                    if data and isinstance(data[0], list):
                        _LOGGER.warning("Indego: migrating old trail format")
                        data = [{"id": i+1, "started": "", "points": s, "stuck": []} for i, s in enumerate(data)]
                    _LOGGER.debug("Loaded %d sessions from disk", len(data))
                    return data
        except Exception as exc:
            _LOGGER.warning("Failed to load trail: %s", exc)
        return []

    def _save_trail(self):
        """Persist sessions to disk, keeping last 4."""
        try:
            path = self._hass.config.path(f"indego_trail_{self._serial}.json")
            with open(path, "w") as f:
                json.dump(self._sessions[-4:], f)
        except Exception as exc:
            _LOGGER.warning("Failed to save trail: %s", exc)

    def _load_stuck(self) -> list:
        """No longer used - stuck points stored per session."""
        return []

    def _save_stuck(self):
        """No longer used - stuck points stored per session."""
        pass

    async def _update_map_svg(self, current_x: int, current_y: int):
        """Fetch SVG map once and overlay session trails and stuck markers."""
        try:
            if self._map_svg is None:
                try:
                    www_path = self._hass.config.path("www")
                    os.makedirs(www_path, exist_ok=True)
                    svg_path = os.path.join(www_path, f"indego_base_{self._serial}.svg")
                    if not os.path.exists(svg_path):
                        _LOGGER.debug("Downloading base map from Bosch API")
                        await self._indego_client.download_map(filename=svg_path)
                    if os.path.exists(svg_path):
                        with open(svg_path, "r") as mf:
                            self._map_svg = mf.read()
                    else:
                        return
                except Exception as map_exc:
                    _LOGGER.warning("Failed to download Indego map: %s %s", type(map_exc).__name__, str(map_exc))
                    return
            if not self._map_svg:
                return

            num_sessions = len(self._sessions)
            overlay_parts = []

            # Green shades from oldest (lightest) to newest (brightest)
            # Each session gets its own distinct shade
            session_colors = [
                "#CCFF90",  # session -3 (oldest): very light green
                "#B2FF59",  # session -2: light green  
                "#69F0AE",  # session -1: medium green
                "#00E676",  # session 0 (newest/current): bright green
            ]

            for s_idx, session in enumerate(self._sessions):
                points = session['points']
                stuck = session.get('stuck', [])
                is_current = (s_idx == num_sessions - 1)

                if len(points) < 2:
                    continue

                age = num_sessions - 1 - s_idx
                opacity = 0.95 / (1.6 ** age)
                stroke_width = max(18 - age * 2, 8)
                color_idx = max(0, len(session_colors) - 1 - age)
                stroke_color = session_colors[color_idx]

                MAX_JUMP = 400
                segments = []
                current_seg = [points[0]]
                for i in range(1, len(points)):
                    dx = points[i][0] - points[i-1][0]
                    dy = points[i][1] - points[i-1][1]
                    dist = (dx*dx + dy*dy) ** 0.5
                    if dist > MAX_JUMP:
                        if len(current_seg) >= 2:
                            segments.append(current_seg)
                        current_seg = [points[i]]
                    else:
                        current_seg.append(points[i])
                if len(current_seg) >= 2:
                    segments.append(current_seg)

                for seg in segments:
                    pts = " ".join(f"{p[0]},{p[1]}" for p in seg)
                    overlay_parts.append(
                        f'<polyline points="{pts}" fill="none" '
                        f'stroke="{stroke_color}" stroke-width="{stroke_width:.1f}" '
                        f'stroke-opacity="{opacity:.2f}" '
                        f'stroke-linecap="round" stroke-linejoin="round"/>'
                    )

                # Stuck markers for this session - same color family as session
                for sx, sy in stuck:
                    overlay_parts.append(
                        f'<circle cx="{sx}" cy="{sy}" r="14" '
                        f'fill="#FF6F00" fill-opacity="{opacity:.2f}" '
                        f'stroke="#E65100" stroke-width="2"/>'
                        f'<text x="{sx}" y="{sy+5}" text-anchor="middle" '
                        f'font-size="14" font-weight="bold" fill="white" fill-opacity="{opacity:.2f}">!</text>'
                    )

            # Current position marker
            overlay_parts.append(
                f'<circle cx="{current_x}" cy="{current_y}" r="12" '
                f'fill="#F44336" stroke="white" stroke-width="2.5"/>'
                f'<circle cx="{current_x}" cy="{current_y}" r="5" fill="white"/>'
            )

            overlay = "".join(overlay_parts)
            annotated = self._map_svg[:self._map_svg.rfind("</svg>")] + f"{overlay}</svg>"

            www_path = self._hass.config.path("www")
            os.makedirs(www_path, exist_ok=True)
            map_path = os.path.join(www_path, f"indego_map_{self._serial}.svg")
            with open(map_path, "w") as f:
                f.write(annotated)
            # Write inline HTML version for cache-free display
            html_path = os.path.join(www_path, f"indego_map_{self._serial}.html")
            with open(html_path, "w") as f:
                f.write(f'''<!DOCTYPE html>
<html><head><style>
body {{ margin: 0; background: transparent; overflow: hidden; }}
svg {{ width: 100%; height: auto; display: block; }}
</style></head><body>{annotated}</body></html>''')

        except Exception as exc:
            _LOGGER.warning("Failed to update Indego map SVG: %s", str(exc))

