"""Platform for Control4 Fan."""
from __future__ import annotations

PARALLEL_UPDATES = 0

from functools import cached_property
import logging
from typing import Any

from pyControl4.fan import C4Fan

from homeassistant.components.fan import (
    FanEntity,
    FanEntityFeature,
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)


from . import Control4Entity, get_items_of_category
from .const import CONF_DIRECTOR, CONF_DYNAMIC_DEVICE_CALLBACKS, CONTROL4_ENTITY_TYPE, Control4ConfigEntry
from .director_utils import director_get_entry_variables

_LOGGER = logging.getLogger(__name__)

CONTROL4_PROXY = "fan"
CONTROL4_CATEGORY = "lights"

async def async_setup_entry(
    hass: HomeAssistant,
    entry: Control4ConfigEntry,
    async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Control4 fans from a config entry."""
    entry_data = entry.runtime_data

    director = entry_data[CONF_DIRECTOR]
    
    items_of_category = await get_items_of_category(hass, entry, CONTROL4_CATEGORY)

    entity_list = []
    setup_attributes = {}

    for item in items_of_category:
        try:
            if item["type"] == CONTROL4_ENTITY_TYPE and item["proxy"] == CONTROL4_PROXY:
                item_name = str(item["name"])
                item_id = item["id"]
                item_area = item["roomName"]
                item_parent_id = item["parentId"]

                item_manufacturer = None
                item_device_name = None
                item_model = None

                for parent_item in items_of_category:
                    if parent_item["id"] == item_parent_id:
                        item_manufacturer = parent_item["manufacturer"]
                        item_device_name = parent_item["name"]
                        item_model = parent_item["model"]

                item_setup_info = await director.get_item_setup(item_id)
                _LOGGER.debug("Fan Setup: %s",str(item_setup_info))
                if 'fan_setup' in item_setup_info:
                    setup_attributes = item_setup_info['fan_setup']
            else:
                continue
        except KeyError:
            _LOGGER.exception(
                "Unknown device properties received from Control4: %s",
                item,
            )
            continue


        item_attributes = await director_get_entry_variables(hass, entry, item_id) | setup_attributes
        _LOGGER.debug("Fan Attributes: %s",str(item_attributes))

        entity_list.append(
            Control4Fan(
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
            )
        )

    async_add_entities(entity_list, True)

    registered_ids: set[int] = {e._idx for e in entity_list}

    async def _async_add_new_fans(hass: HomeAssistant, entry: Control4ConfigEntry) -> None:
        new_items = await get_items_of_category(hass, entry, CONTROL4_CATEGORY)
        new_entities = []
        entry_data = entry.runtime_data
        director = entry_data[CONF_DIRECTOR]
        for item in new_items:
            try:
                if not (item["type"] == CONTROL4_ENTITY_TYPE and item["proxy"] == CONTROL4_PROXY):
                    continue
                if item["id"] in registered_ids:
                    continue
                item_manufacturer = None
                item_device_name = None
                item_model = None
                for parent_item in new_items:
                    if parent_item["id"] == item["parentId"]:
                        item_manufacturer = parent_item.get("manufacturer")
                        item_device_name = parent_item.get("name")
                        item_model = parent_item.get("model")
                setup_attributes = {}
                try:
                    item_setup_info = await director.get_item_setup(item["id"])
                    if "fan_setup" in item_setup_info:
                        setup_attributes = item_setup_info["fan_setup"]
                except Exception:
                    pass
                item_attributes = await director_get_entry_variables(hass, entry, item["id"]) | setup_attributes
                new_entities.append(
                    Control4Fan(
                        entry_data,
                        entry,
                        str(item["name"]),
                        item["id"],
                        item_device_name,
                        item_manufacturer,
                        item_model,
                        item["parentId"],
                        item["roomName"],
                        item_attributes,
                    )
                )
                registered_ids.add(item["id"])
            except KeyError:
                _LOGGER.exception("Unknown fan device properties: %s", item)
        if new_entities:
            async_add_entities(new_entities, True)

    entry.runtime_data[CONF_DYNAMIC_DEVICE_CALLBACKS].append(_async_add_new_fans)


class Control4Fan(Control4Entity, FanEntity):  # type: ignore[misc]
    """Control4 fan entity."""

    def create_api_object(self):
        """Create a pyControl4 device object.
           This exists so the director token used is always the
           latest one, without needing to re-init the entire entity.
        """
        return C4Fan(self.entry_data[CONF_DIRECTOR], self._idx)
    
    async def _update_callback(self, device, message):
        """Update state attributes in hass after receiving a Websocket update for our item id/parent device id."""
        # Message will be False when a Websocket disconnect is detected
        _LOGGER.debug("Turn ON Fan Attributes: %s",str(message))
        if message is False:
            self._attr_available = False
        elif message["evtName"] == "OnDataToUI":
            self._attr_available = True
            data = message["data"]
            if "fan_state" in data:
                self._extra_state_attributes["current_speed"] = data["fan_state"].pop("current_speed")
                self._extra_state_attributes["directions"] = data["fan_state"].pop("is_reversed")
                await self._data_to_extra_state_attributes(data["fan_state"])
            else:
                _LOGGER.error("Unknown fan state data: %s", data)
                await self._data_to_extra_state_attributes(data)

        _LOGGER.debug("Message for device %s", device)
        self.async_write_ha_state()

    @property
    def percentage_step(self) -> float:
        """Return the step size for percentage."""
        return 100/self._extra_state_attributes["speeds_count"]

    @property
    def percentage(self) -> int | None:  # type: ignore[override]
        """Return the current speed as a percentage."""
        if "current_speed" in self._extra_state_attributes:
            return ranged_value_to_percentage(
                (1, self._extra_state_attributes["speeds_count"]), 
                 self._extra_state_attributes["current_speed"]
            )
        return ranged_value_to_percentage(
            (1, self._extra_state_attributes["speeds_count"]), 
              self._extra_state_attributes["CURRENT_SPEED"]
        )

    @property
    def is_on(self):
        """Return whether this fan is on or off."""
        for key in ("current_speed", "CURRENT_SPEED"):
            speed = self._extra_state_attributes.get(key)
            if speed is not None:
                return speed != 0
        return False  # If both are None, assume off


    @property
    def preset_modes(self):  # type: ignore[override]
        """Return a list of available modes for the fan."""
        return [str(x) for x in range(0, self._extra_state_attributes["speeds_count"]+1)]

    @property
    def preset_mode(self):  # type: ignore[override]
        """Return the current preset mode of this fan."""
        return str(self._extra_state_attributes["preset_speed"])

    @cached_property
    def supported_features(self) -> FanEntityFeature:
        """Flag supported features."""
        return FanEntityFeature.PRESET_MODE | FanEntityFeature.SET_SPEED | FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF


    async def async_turn_on(self, percentage: int | None = None, preset_mode: str | None = None, **kwargs: Any) -> None:
        """Turn the entity on."""
        _LOGGER.debug("Turn ON Fan Attributes: %s",str(self._extra_state_attributes))

        c4_fan = self.create_api_object()

        if percentage is not None:
            speed = int(percentage_to_ranged_value((1, self._extra_state_attributes["speeds_count"]), percentage))
            await c4_fan.set_speed(speed)
        elif preset_mode is not None:
            await c4_fan.set_speed(int(preset_mode))
        elif self._extra_state_attributes["preset_speed"] != 0:
            await c4_fan.set_speed(self._extra_state_attributes["preset_speed"])
        else:
            await c4_fan.set_speed(1)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the preset mode, the speed the fan comes on to."""
        c4_fan = self.create_api_object()
        await c4_fan.set_preset(int(preset_mode))

    async def async_set_percentage(self, percentage: int) -> None:
        """Set a percentage speed for the fan comes on to."""
        c4_fan = self.create_api_object()
        speed = int(percentage_to_ranged_value((1, self._extra_state_attributes["speeds_count"]), percentage))
        await c4_fan.set_speed(speed)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the entity off."""
        c4_fan = self.create_api_object()
        await c4_fan.set_speed(0)

    async def async_toggle(self, **kwargs: Any) -> None:
        """Toggle the fan."""
        if self.is_on:
            await self.async_turn_off()
        else:
            await self.async_turn_on()


