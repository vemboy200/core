"""Platform for Control4 Alarm Control Panel."""

from __future__ import annotations

PARALLEL_UPDATES = 0

from functools import cached_property
import logging

from pyControl4.alarm import C4SecurityPanel
import voluptuous

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
)
from homeassistant.components.alarm_control_panel.const import (
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_platform

from . import Control4Entity, get_items_of_category
from .const import (
    CONF_ALARM_ARM_STATES,
    CONF_ALARM_AWAY_MODE,
    CONF_ALARM_CUSTOM_BYPASS_MODE,
    CONF_ALARM_HOME_MODE,
    CONF_ALARM_NIGHT_MODE,
    CONF_ALARM_VACATION_MODE,
    CONF_DIRECTOR,
    CONTROL4_ENTITY_TYPE,
    Control4ConfigEntry,
    DEFAULT_ALARM_AWAY_MODE,
    DEFAULT_ALARM_CUSTOM_BYPASS_MODE,
    DEFAULT_ALARM_HOME_MODE,
    DEFAULT_ALARM_NIGHT_MODE,
    DEFAULT_ALARM_VACATION_MODE,
)
from .director_utils import director_get_entry_variables

_LOGGER = logging.getLogger(__name__)

CONTROL4_CATEGORY = "security"

CONTROL4_ARMED_AWAY_VAR = "AWAY_STATE"
CONTROL4_ARMED_HOME_VAR = "HOME_STATE"
CONTROL4_DISARMED_VAR = "DISARMED_STATE"
CONTROL4_ALARM_STATE_VAR = "ALARM_STATE"
CONTROL4_DISPLAY_TEXT_VAR = "DISPLAY_TEXT"
CONTROL4_TROUBLE_TEXT_VAR = "TROUBLE_TEXT"
CONTROL4_PARTITION_STATE_VAR = "PARTITION_STATE"
CONTROL4_DELAY_TIME_REMAINING_VAR = "DELAY_TIME_REMAINING"
CONTROL4_OPEN_ZONE_COUNT_VAR = "OPEN_ZONE_COUNT"
CONTROL4_ALARM_TYPE_VAR = "ALARM_TYPE"
CONTROL4_ARMED_TYPE_VAR = "ARMED_TYPE"
CONTROL4_LAST_EMERGENCY_VAR = "LAST_EMERGENCY"
CONTROL4_LAST_ARM_FAILURE_VAR = "LAST_ARM_FAILED"

CONTROL4_EXIT_DELAY_STATE = "EXIT_DELAY"
CONTROL4_ENTRY_DELAY_STATE = "ENTRY_DELAY"
CONTROL4_ARMED_STATE = "ARMED"
CONTROL4_DISARMED_NOT_READY_STATE = "DISARMED_NOT_READY"
CONTROL4_DISARMED_READY_STATE = "DISARMED_READY"

CONTROL4_PARTITION_STATE_DATA_MAPPING = {
    "state": "PARTITION_STATE",
    "trouble": "TROUBLE_TEXT",
    "text": "DISPLAY_TEXT",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: Control4ConfigEntry, async_add_entities
):
    """Set up Control4 alarm control panel from a config entry."""
    # Register alarm_control_panel specific service
    platform = entity_platform.current_platform.get()
    if platform is not None:
        platform.async_register_entity_service(
            "send_alarm_keystrokes",
            {voluptuous.Required("keystrokes"): cv.string},
            "send_alarm_keystrokes",
        )

    entry_data = entry.runtime_data

    items_of_category = await get_items_of_category(hass, entry, CONTROL4_CATEGORY)

    entity_list = []

    director = entry_data[CONF_DIRECTOR]

    for item in items_of_category:
        try:
            if item["type"] == CONTROL4_ENTITY_TYPE and item["id"]:
                if "capabilities" in item and "arm_states" in item["capabilities"]:
                    entry_data[CONF_ALARM_ARM_STATES].update(
                        item["capabilities"]["arm_states"].split(",")
                    )
                item_name = str(item["name"])
                item_id = item["id"]
                item_area = item["roomName"]
                item_parent_id = item["parentId"]

                item_manufacturer = None
                item_device_name = None
                item_model = None

                try:
                    item_setup_info = await director.get_item_setup(item_id)
                    item_enabled = item_setup_info.get("setup", {}).get("enabled", True)
                except (KeyError, TypeError):
                    _LOGGER.debug(
                        "No setup info available for device %s, defaulting to enabled",
                        item_name,
                    )
                    item_enabled = True

                for parent_item in items_of_category:
                    if parent_item["id"] == item_parent_id:
                        item_manufacturer = parent_item.get("manufacturer")
                        item_device_name = parent_item.get("name")
                        item_model = parent_item.get("model")
            else:
                continue
        except KeyError as exception:
            _LOGGER.debug(
                "Unknown device properties received from Control4: %s %s",
                exception,
                item,
            )
            continue

        item_attributes = await director_get_entry_variables(hass, entry, item_id)

        c4_alarm = C4SecurityPanel(director, item_id)
        item_emergency_types = await c4_alarm.get_emergency_types()

        entity_list.append(
            Control4AlarmControlPanel(
                entry_data,
                entry,
                item_name,
                item_id,
                item_device_name,
                item_manufacturer,
                item_model,
                item_parent_id,
                item_area,
                item_attributes,
                item_enabled,
                item_emergency_types,
            )
        )

    async_add_entities(entity_list, True)


class Control4AlarmControlPanel(Control4Entity, AlarmControlPanelEntity):  # type: ignore[misc]
    """Control4 alarm control panel entity."""

    def __init__(
        self,
        entry_data: dict,
        entry: Control4ConfigEntry,
        name: str,
        idx: int,
        device_name: str | None,
        device_manufacturer: str | None,
        device_model: str | None,
        device_id: int,
        device_area: str,
        device_attributes: dict,
        is_enabled: bool,
        emergency_types: list[str],
    ) -> None:
        """Initialize Control4 alarm control panel entity."""
        super().__init__(
            entry_data,
            entry,
            name,
            idx,
            device_name,
            device_manufacturer,
            device_model,
            device_id,
            device_area,
            device_attributes,
        )
        self._is_enabled = is_enabled
        self._emergency_types = emergency_types
        self._extra_state_attributes["zone_state"] = {}

    async def _update_callback(self, device, message):
        """Update state attributes in hass after receiving a Websocket update for our item id/parent device id."""
        _LOGGER.debug(message)

        # Message will be False when a Websocket disconnect is detected
        if message is False:
            self._attr_available = False
        elif message["evtName"] == "OnDataToUI":
            self._attr_available = True
            data = message["data"]
            # Extra handling for alarm specific messages
            if "partition_state" in data:
                data = data["partition_state"]
                for key, value in data.items():
                    if key in CONTROL4_PARTITION_STATE_DATA_MAPPING:
                        self._extra_state_attributes[
                            CONTROL4_PARTITION_STATE_DATA_MAPPING[key]
                        ] = value
                    else:
                        self._extra_state_attributes[key.upper()] = value
            elif "text" in data:
                self._extra_state_attributes[
                    CONTROL4_PARTITION_STATE_DATA_MAPPING["text"]
                ] = data["text"]
            elif "zone_state" in data:
                data = data["zone_state"]
                self._extra_state_attributes["zone_state"][data["id"]] = data
            elif "devicecommand" in data:
                data = data["devicecommand"]["params"]
                await self._data_to_extra_state_attributes(data)
            else:
                await self._data_to_extra_state_attributes(data)
        _LOGGER.debug("Message for device %s", device)
        self.async_write_ha_state()

    def create_api_object(self):
        """Create a pyControl4 device object.

        This exists so the director token used is always the latest one, without needing to re-init the entire entity.
        """
        return C4SecurityPanel(self.entry_data[CONF_DIRECTOR], self._idx)

    @cached_property
    def entity_registry_enabled_default(self) -> bool:
        """Return if the entity should be enabled when first added to the entity registry."""
        return self._is_enabled

    @cached_property
    def code_format(self):
        """Regex for code format or None if no code is required."""
        return CodeFormat.NUMBER

    @cached_property
    def supported_features(self) -> AlarmControlPanelEntityFeature:
        """Flag supported features."""
        flags = AlarmControlPanelEntityFeature(0)
        if not self.entry_data[CONF_ALARM_AWAY_MODE] == DEFAULT_ALARM_AWAY_MODE:
            flags |= AlarmControlPanelEntityFeature.ARM_AWAY
        if not self.entry_data[CONF_ALARM_HOME_MODE] == DEFAULT_ALARM_HOME_MODE:
            flags |= AlarmControlPanelEntityFeature.ARM_HOME
        if not self.entry_data[CONF_ALARM_NIGHT_MODE] == DEFAULT_ALARM_NIGHT_MODE:
            flags |= AlarmControlPanelEntityFeature.ARM_NIGHT
        if (
            not self.entry_data[CONF_ALARM_CUSTOM_BYPASS_MODE]
            == DEFAULT_ALARM_CUSTOM_BYPASS_MODE
        ):
            flags |= AlarmControlPanelEntityFeature.ARM_CUSTOM_BYPASS
        if not self.entry_data[CONF_ALARM_VACATION_MODE] == DEFAULT_ALARM_VACATION_MODE:
            flags |= AlarmControlPanelEntityFeature.ARM_VACATION
        if self._emergency_types:
            flags |= AlarmControlPanelEntityFeature.TRIGGER
        return flags

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:  # type: ignore[override]
        """Return the state of the device."""
        partition_state = self.extra_state_attributes.get(CONTROL4_PARTITION_STATE_VAR)
        if partition_state == CONTROL4_EXIT_DELAY_STATE:
            return AlarmControlPanelState.ARMING
        if partition_state == CONTROL4_ENTRY_DELAY_STATE:
            return AlarmControlPanelState.PENDING
        if (
            partition_state == CONTROL4_DISARMED_NOT_READY_STATE
            or partition_state == CONTROL4_DISARMED_READY_STATE
        ):
            return AlarmControlPanelState.DISARMED
        if partition_state == CONTROL4_ARMED_STATE:
            armed_type = self.extra_state_attributes.get(CONTROL4_ARMED_TYPE_VAR)
            if armed_type == self.entry_data[CONF_ALARM_AWAY_MODE]:
                return AlarmControlPanelState.ARMED_AWAY
            if armed_type == self.entry_data[CONF_ALARM_HOME_MODE]:
                return AlarmControlPanelState.ARMED_HOME
            if armed_type == self.entry_data[CONF_ALARM_NIGHT_MODE]:
                return AlarmControlPanelState.ARMED_NIGHT
            if armed_type == self.entry_data[CONF_ALARM_CUSTOM_BYPASS_MODE]:
                return AlarmControlPanelState.ARMED_CUSTOM_BYPASS
            if armed_type == self.entry_data[CONF_ALARM_VACATION_MODE]:
                return AlarmControlPanelState.ARMED_VACATION
        alarm_state = self.extra_state_attributes.get(CONTROL4_ALARM_TYPE_VAR)
        if alarm_state:
            return AlarmControlPanelState.TRIGGERED

        return None

    async def async_alarm_arm_away(self, code=None):
        """Send arm away command."""
        try:
            await self.create_api_object().set_arm(code or "", self.entry_data[CONF_ALARM_AWAY_MODE])
        except Exception as err:
            raise HomeAssistantError(f"Control4 alarm arm away failed: {err}") from err

    async def async_alarm_arm_home(self, code=None):
        """Send arm home command."""
        try:
            await self.create_api_object().set_arm(code or "", self.entry_data[CONF_ALARM_HOME_MODE])
        except Exception as err:
            raise HomeAssistantError(f"Control4 alarm arm home failed: {err}") from err

    async def async_alarm_arm_night(self, code=None):
        """Send arm night command."""
        try:
            await self.create_api_object().set_arm(code or "", self.entry_data[CONF_ALARM_NIGHT_MODE])
        except Exception as err:
            raise HomeAssistantError(f"Control4 alarm arm night failed: {err}") from err

    async def async_alarm_arm_custom_bypass(self, code=None):
        """Send arm custom bypass command."""
        try:
            await self.create_api_object().set_arm(code or "", self.entry_data[CONF_ALARM_CUSTOM_BYPASS_MODE])
        except Exception as err:
            raise HomeAssistantError(f"Control4 alarm arm custom bypass failed: {err}") from err

    async def async_alarm_arm_vacation(self, code=None):
        """Send arm vacation command."""
        try:
            await self.create_api_object().set_arm(code or "", self.entry_data[CONF_ALARM_VACATION_MODE])
        except Exception as err:
            raise HomeAssistantError(f"Control4 alarm arm vacation failed: {err}") from err

    async def async_alarm_disarm(self, code=None):
        """Send disarm command."""
        try:
            await self.create_api_object().set_disarm(code or "")
        except Exception as err:
            raise HomeAssistantError(f"Control4 alarm disarm failed: {err}") from err

    async def async_alarm_trigger(self, code=None):
        """Send trigger/emergency command."""
        if not self._emergency_types:
            return
        preferred_order = ["Police", "Fire", "Medical", "Panic"]
        emergency_type = next(
            (t for t in preferred_order if t in self._emergency_types),
            self._emergency_types[0],
        )
        try:
            await self.create_api_object().trigger_emergency(emergency_type)
        except Exception as err:
            raise HomeAssistantError(f"Control4 alarm trigger failed: {err}") from err

    async def send_alarm_keystrokes(self, keystrokes):
        """Send custom keystrokes."""
        c4_alarm = self.create_api_object()
        try:
            for key in keystrokes:
                await c4_alarm.send_key_press(key)
        except Exception as err:
            raise HomeAssistantError(f"Control4 alarm keystrokes failed: {err}") from err
