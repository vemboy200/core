"""Platform for Control4 Locks."""
from __future__ import annotations

PARALLEL_UPDATES = 0

import logging

from pyControl4.relay import C4Relay

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import Control4Entity, get_items_of_category
from .const import CONF_DIRECTOR, CONF_DYNAMIC_DEVICE_CALLBACKS, CONTROL4_ENTITY_TYPE, Control4ConfigEntry
from .director_utils import director_get_entry_variables

_LOGGER = logging.getLogger(__name__)

CONTROL4_CATEGORY = "locks"


async def async_setup_entry(
    hass: HomeAssistant, entry: Control4ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Control4 locks from a config entry."""
    entry_data = entry.runtime_data

    items_of_category = await get_items_of_category(hass, entry, CONTROL4_CATEGORY)

    entity_list = []

    for item in items_of_category:
        try:
            if item["type"] == CONTROL4_ENTITY_TYPE and item["id"]:
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
            else:
                continue
        except KeyError:
            _LOGGER.exception(
                "Unknown device properties received from Control4: %s",
                item,
            )
            continue

        item_attributes = await director_get_entry_variables(hass, entry, item_id)

        # Ignore locks that are not setup as basic relays as they are currently unsupported
        if "RelayState" in item_attributes:
            entity_list.append(
                Control4Lock(
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

    async def _async_add_new_locks(hass: HomeAssistant, entry: Control4ConfigEntry) -> None:
        new_items = await get_items_of_category(hass, entry, CONTROL4_CATEGORY)
        new_entities = []
        entry_data = entry.runtime_data
        for item in new_items:
            try:
                if not (item["type"] == CONTROL4_ENTITY_TYPE and item["id"]):
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
                item_attributes = await director_get_entry_variables(hass, entry, item["id"])
                if "RelayState" not in item_attributes:
                    continue
                new_entities.append(
                    Control4Lock(
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
                _LOGGER.exception("Unknown lock device properties: %s", item)
        if new_entities:
            async_add_entities(new_entities, True)

    entry.runtime_data[CONF_DYNAMIC_DEVICE_CALLBACKS].append(_async_add_new_locks)


class Control4Lock(Control4Entity, LockEntity):  # type: ignore[misc]
    """Control4 lock entity."""

    def create_api_object(self):
        """Create a pyControl4 device object.

        This exists so the director token used is always the latest one, without needing to re-init the entire entity.
        """
        return C4Relay(self.entry_data[CONF_DIRECTOR], self._idx)

    async def _update_callback(self, device, message):
        """Update state attributes in hass after receiving a Websocket update for our item id/parent device id."""
        # Message will be False when a Websocket disconnect is detected
        if message is False:
            self._attr_available = False
        elif message["evtName"] == "OnDataToUI":
            self._attr_available = True
            data = message["data"]
            if "relay_state" in data:
                current_state = data["relay_state"].pop("current_state")
                if current_state == "CLOSED":
                    self._extra_state_attributes["RelayState"] = 1
                elif current_state == "OPENED":
                    self._extra_state_attributes["RelayState"] = 0
                else:
                    _LOGGER.error("Unkonwn relay state %s", current_state)
                await self._data_to_extra_state_attributes(data["relay_state"])

        _LOGGER.debug("Message for device %s", device)
        self.async_write_ha_state()

    @property
    def is_locked(self):  # type: ignore[override]
        """Return whether the lock is locked or unlocked. An open relay (0) typically means it is locked."""
        if "RelayState" in self._extra_state_attributes:
            return self._extra_state_attributes["RelayState"] == 0

    async def async_lock(self, **kwargs):
        """Lock the lock. Assume no code is required, but unsure if that is true of all locks in control4"""
        c4_relay = self.create_api_object()
        await c4_relay.open()

    async def async_unlock(self, **kwargs):
        """Unlock the lock. Assume no code is required, but unsure if that is true of all locks in control4"""
        c4_relay = self.create_api_object()
        await c4_relay.close()
