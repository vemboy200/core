"""Platform for Control4 Binary Sensor."""
from __future__ import annotations

PARALLEL_UPDATES = 0

from datetime import datetime
from functools import cached_property
import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later

from . import Control4Entity, get_items_of_category
from .const import (
    CONF_DIRECTOR,
    CONF_DIRECTOR_ALL_ITEMS,
    CONF_DYNAMIC_DEVICE_CALLBACKS,
    CONF_DYNALITE_ENABLED,
    CONF_CONTROLLER_UNIQUE_ID,
    CONTROL4_ENTITY_TYPE,
    Control4ConfigEntry,
    DOMAIN,
)
from .director_utils import director_get_entry_variables

_LOGGER = logging.getLogger(__name__)

CONTROL4_CATEGORY = "sensors"
CONTROL4_CONTROL_TYPE = "control4_contactsingle"
CONTROL4_SENSOR_VAR = "ContactState"

CONTROL4_DOOR_PROXY = "contactsingle_doorcontactsensor_c4"
CONTROL4_WINDOW_PROXY = "contactsingle_windowcontactsensor_c4"
CONTROL4_MOTION_PROXY = "contactsingle_motionsensor_c4"
CONTROL4_GARAGE_DOOR_PROXY = "relaycontact_garagedoor_c4"
# Legacy / third-party motion-sensor proxy used by older Generic Motion Sensor
# drivers and NYCE 3041/3043 zigbee motion sensors (no _c4 suffix).
CONTROL4_LEGACY_MOTION_PROXY = "contactsingle_motionsensor"
# NYCE 3011 zigbee door/window contact sensor.
CONTROL4_NYCE_DOORWINDOW_PROXY = "DoorWindow_zb_nyce_3011"
# Pump/jet feedback contact — ContactState=true means relay is closed = pump
# is RUNNING (opposite convention from door/window/motion contacts).
CONTROL4_CONTACTSWITCH_PROXY = "contactsingle_contactswitch_c4"

# Proxy types where ContactState=true should be reported as binary_sensor "on"
# (NOT inverted). Default behaviour for all others is to invert.
NON_INVERTED_CONTACT_PROXIES = {
    CONTROL4_CONTACTSWITCH_PROXY,
}

# List of proxy types that should be handled as switches instead of binary sensors
CONTROL4_RELAY_PROXY_TYPES = {
    "relaysingle_relay_c4",
    "relaysingle_doorlock_c4",
    "cardaccess_wirelessrelay",
    "relaysingle_electronicgate_c4",
    "relaysingle_pump_c4",
}

CONTROL4_PROXY_MAPPING = {
    CONTROL4_DOOR_PROXY: BinarySensorDeviceClass.DOOR,
    CONTROL4_WINDOW_PROXY: BinarySensorDeviceClass.WINDOW,
    CONTROL4_MOTION_PROXY: BinarySensorDeviceClass.MOTION,
    CONTROL4_GARAGE_DOOR_PROXY: BinarySensorDeviceClass.GARAGE_DOOR,
    CONTROL4_LEGACY_MOTION_PROXY: BinarySensorDeviceClass.MOTION,
    CONTROL4_NYCE_DOORWINDOW_PROXY: BinarySensorDeviceClass.DOOR,
    CONTROL4_CONTACTSWITCH_PROXY: BinarySensorDeviceClass.RUNNING,
}

DYNALITE_TRIGGER_PROXY = "dynalite_trigger"
TRIGGER_RESET_SEC = 2


async def async_setup_entry(
    hass: HomeAssistant, entry: Control4ConfigEntry, async_add_entities
):
    """Set up Control4 binary sensor from a config entry."""

    entry_data = entry.runtime_data
    director_all_items = entry_data[CONF_DIRECTOR_ALL_ITEMS]

    # Get items from sensors category
    items_of_category = await get_items_of_category(hass, entry, CONTROL4_CATEGORY)
    _LOGGER.debug("Found %d items in sensors category", len(items_of_category))

    # Add garage door sensors from devices category, but only if they're not already in sensors
    existing_ids = {item["id"] for item in items_of_category}
    garage_door_sensors = [
        item for item in director_all_items
        if item.get("proxy") == CONTROL4_GARAGE_DOOR_PROXY and item["id"] not in existing_ids
    ]
    
    items_of_category.extend(garage_door_sensors)

    entity_list = []
    seen_ids = set()  # Track unique IDs to prevent duplicates

    director = entry_data[CONF_DIRECTOR]

    for item in items_of_category:
        try:
            if item["type"] == CONTROL4_ENTITY_TYPE and item["id"]:
                # Skip if this is a relay device (except garage door sensors)
                if item.get("proxy") in CONTROL4_RELAY_PROXY_TYPES:
                    _LOGGER.debug("Skipping relay device: %s", item.get("proxy"))
                    continue

                item_name = str(item["name"])
                item_id = item["id"]
                item_area = item["roomName"]
                item_parent_id = item["parentId"]
                item_proxy = item.get("proxy", "")
                _LOGGER.debug("Processing device: %s (proxy: %s)", item_name, item_proxy)

                # Generate a unique ID that includes all relevant information
                unique_id = f"{entry.entry_id}_{item_id}_{item_proxy}_{item_name}"
                if unique_id in seen_ids:
                    _LOGGER.warning(
                        "Duplicate unique ID detected for %s, skipping", item_name
                    )
                    continue
                seen_ids.add(unique_id)

                item_manufacturer = None
                item_device_name = None
                item_model = None

                item_device_class = BinarySensorDeviceClass.OPENING
                for proxy_type in [
                    CONTROL4_DOOR_PROXY,
                    CONTROL4_WINDOW_PROXY,
                    CONTROL4_MOTION_PROXY,
                    CONTROL4_GARAGE_DOOR_PROXY,
                    CONTROL4_LEGACY_MOTION_PROXY,
                    CONTROL4_NYCE_DOORWINDOW_PROXY,
                    CONTROL4_CONTACTSWITCH_PROXY,
                ]:
                    if item["proxy"] == proxy_type:
                        item_device_class = CONTROL4_PROXY_MAPPING[proxy_type]
                        _LOGGER.debug("Found device class %s for %s", item_device_class, item_name)
                        break

                item_setup_info = await director.get_item_setup(item_id)
                item_alarm_zone_id = None
                if "panel_setup" in item_setup_info:
                    for key in item_setup_info["panel_setup"]["all_zones"]["zone_info"]:
                        if key["name"] == item_name:
                            item_alarm_zone_id = key["id"]
                            break

                for parent_item in items_of_category:
                    if parent_item["id"] == item_parent_id:
                        item_manufacturer = parent_item["manufacturer"]
                        item_device_name = parent_item["name"]
                        item_model = parent_item["model"]
            else:
                continue
        except KeyError:
            _LOGGER.warning(
                "Unknown device properties received from Control4: %s",
                item,
            )
            continue

        item_attributes = await director_get_entry_variables(hass, entry, item_id)
        _LOGGER.debug("Device attributes for %s: %s", item_name, item_attributes)

        entity_list.append(
            Control4BinarySensor(
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
                item_device_class,
                int(item_alarm_zone_id) if item_alarm_zone_id is not None else None,
                item_proxy,
                unique_id,
            )
        )

    # Dynalite trigger binary sensors (updated by TCP listener when enabled)
    if entry_data.get(CONF_DYNALITE_ENABLED):
        for item in director_all_items:
            if (
                item.get("type") == CONTROL4_ENTITY_TYPE
                and item.get("id")
                and item.get("proxy") == DYNALITE_TRIGGER_PROXY
            ):
                item_id = item["id"]
                item_name = str(item.get("name", f"Dynalite {item_id}"))
                item_area = item.get("roomName", "")
                item_parent_id = item.get("parentId")
                item_device_name = None
                item_manufacturer = item.get("manufacturer")
                item_model = item.get("model")
                for parent_item in director_all_items:
                    if parent_item.get("id") == item_parent_id:
                        item_device_name = parent_item.get("name")
                        break
                entity_list.append(
                    Control4DynaliteTriggerBinarySensor(
                        entry_data=entry_data,
                        entry=entry,
                        name=item_name,
                        idx=item_id,
                        device_name=item_device_name,
                        device_manufacturer=item_manufacturer,
                        device_model=item_model,
                        device_id=item_parent_id or 0,
                        device_area=item_area,
                    )
                )

    async_add_entities(entity_list, True)

    registered_unique_ids: set[str] = {e._attr_unique_id for e in entity_list}
    registered_dynalite_ids: set[int] = {
        e._idx for e in entity_list
        if isinstance(e, Control4DynaliteTriggerBinarySensor)
    }

    async def _async_add_new_binary_sensors(hass: HomeAssistant, entry: Control4ConfigEntry) -> None:
        entry_data = entry.runtime_data
        new_entities = []

        # Contact sensors from director category
        new_items = await get_items_of_category(hass, entry, CONTROL4_CATEGORY)
        new_garage_sensors = [
            item for item in entry_data[CONF_DIRECTOR_ALL_ITEMS]
            if item.get("proxy") == CONTROL4_GARAGE_DOOR_PROXY
            and item["id"] not in {it["id"] for it in new_items}
        ]
        all_items = new_items + new_garage_sensors
        director = entry_data[CONF_DIRECTOR]

        for item in all_items:
            try:
                if not (item["type"] == CONTROL4_ENTITY_TYPE and item["id"]):
                    continue
                if item.get("proxy") in CONTROL4_RELAY_PROXY_TYPES:
                    continue
                proxy = item.get("proxy", "")
                uid = f"{entry.entry_id}_{item['id']}_{proxy}_{item['name']}"
                if uid in registered_unique_ids:
                    continue
                item_device_class = BinarySensorDeviceClass.OPENING
                for proxy_type, dc in CONTROL4_PROXY_MAPPING.items():
                    if proxy == proxy_type:
                        item_device_class = dc
                        break
                item_setup_info = await director.get_item_setup(item["id"])
                alarm_zone_id = None
                if "panel_setup" in item_setup_info:
                    for z in item_setup_info["panel_setup"]["all_zones"]["zone_info"]:
                        if z["name"] == item["name"]:
                            alarm_zone_id = z["id"]
                            break
                item_manufacturer = None
                item_device_name = None
                item_model = None
                for parent_item in all_items:
                    if parent_item["id"] == item["parentId"]:
                        item_manufacturer = parent_item.get("manufacturer")
                        item_device_name = parent_item.get("name")
                        item_model = parent_item.get("model")
                item_attributes = await director_get_entry_variables(hass, entry, item["id"])
                new_entities.append(
                    Control4BinarySensor(
                        entry_data, entry,
                        str(item["name"]), item["id"],
                        item_device_name, item_manufacturer, item_model,
                        item["parentId"], item["roomName"], item_attributes,
                        item_device_class,
                        int(alarm_zone_id) if alarm_zone_id is not None else None,
                        proxy, uid,
                    )
                )
                registered_unique_ids.add(uid)
            except KeyError:
                _LOGGER.warning("Unknown binary sensor device properties: %s", item)

        # Dynalite triggers
        if entry_data.get(CONF_DYNALITE_ENABLED):
            for item in entry_data[CONF_DIRECTOR_ALL_ITEMS]:
                if (
                    item.get("type") == CONTROL4_ENTITY_TYPE
                    and item.get("id")
                    and item.get("proxy") == DYNALITE_TRIGGER_PROXY
                    and item["id"] not in registered_dynalite_ids
                ):
                    item_id = item["id"]
                    item_device_name = None
                    for parent_item in entry_data[CONF_DIRECTOR_ALL_ITEMS]:
                        if parent_item.get("id") == item.get("parentId"):
                            item_device_name = parent_item.get("name")
                            break
                    new_entities.append(
                        Control4DynaliteTriggerBinarySensor(
                            entry_data=entry_data, entry=entry,
                            name=str(item.get("name", f"Dynalite {item_id}")),
                            idx=item_id,
                            device_name=item_device_name,
                            device_manufacturer=item.get("manufacturer"),
                            device_model=item.get("model"),
                            device_id=item.get("parentId") or 0,
                            device_area=item.get("roomName", ""),
                        )
                    )
                    registered_dynalite_ids.add(item_id)

        if new_entities:
            async_add_entities(new_entities, True)

    entry.runtime_data[CONF_DYNAMIC_DEVICE_CALLBACKS].append(_async_add_new_binary_sensors)


class Control4BinarySensor(Control4Entity, BinarySensorEntity):  # type: ignore[misc]
    """Control4 binary sensor entity."""

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
        device_class: BinarySensorDeviceClass,
        alarm_zone_id: int | None,
        proxy_type: str,
        unique_id: str,
    ) -> None:
        """Initialize Control4 binary sensor entity."""
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
        self._device_class = device_class
        self._proxy_type = proxy_type
        self._extra_state_attributes["alarm_zone_id"] = alarm_zone_id
        self._attr_available = True
        self._attr_unique_id = unique_id
        
        # Initialize state from attributes
        if "ContactState" in self._extra_state_attributes:
            self._extra_state_attributes["ContactState"] = bool(
                self._extra_state_attributes["ContactState"]
            )
        elif "RelayState" in self._extra_state_attributes:
            # For garage door sensors, use RelayState instead of ContactState
            self._extra_state_attributes["ContactState"] = bool(
                self._extra_state_attributes["RelayState"]
            )
            
        self._extra_state_attributes["StateVerified"] = bool(
            self._extra_state_attributes.get("StateVerified", True)
        )

    async def _update_callback(self, device, message):
        """Update state attributes in hass after receiving a Websocket
        update for our item id/parent device id."""

        # Message will be False when a Websocket disconnect is detected
        if message is False:
            self._attr_available = False
        elif message["evtName"] == "OnDataToUI":
            self._attr_available = True
            data = message["data"]
            # Extra handling for alarm specific messages
            if "zone_state" in data:
                self._extra_state_attributes["ContactState"] = bool(
                    not data["zone_state"].pop("is_open")
                )
                self._extra_state_attributes["LastActionTime"] = message["time"]

            if "contact_state" in data:
                self._extra_state_attributes["ContactState"] = bool(
                    data["contact_state"].pop("current_state") == "CLOSED"
                )
                self._extra_state_attributes["StateVerified"] = data[
                    "contact_state"
                ].pop("is_verified")
                self._extra_state_attributes["LastActionTime"] = message["time"]
                await self._data_to_extra_state_attributes(data["contact_state"])

            if "relay_state" in data:
                # For garage door sensors, use relay_state instead of contact_state
                self._extra_state_attributes["ContactState"] = bool(
                    data["relay_state"].pop("current_state") == "CLOSED"
                )
                self._extra_state_attributes["StateVerified"] = data["relay_state"].pop(
                    "is_verified"
                )
                self._extra_state_attributes["LastActionTime"] = message["time"]
                await self._data_to_extra_state_attributes(data["relay_state"])

        _LOGGER.debug("Updated state for %s: %s", self.name, self._extra_state_attributes)
        self.async_write_ha_state()

    @property
    def is_on(self):  # type: ignore[override]
        """Return true if the binary sensor is on.

        Most contact-style proxies (door / window / motion / garage) use
        Control4's convention where ContactState=true means the contact is
        closed (idle), so we invert to give HA's "on=event/active"
        convention.

        Pump-feedback contacts (proxy contactsingle_contactswitch_c4) wire
        the relay so that ContactState=true means the relay is closed =
        pump is RUNNING. For those proxies (listed in
        NON_INVERTED_CONTACT_PROXIES) we report ContactState as-is so
        device_class=running reads correctly.
        """
        if "ContactState" in self._extra_state_attributes:
            contact_state = bool(self.extra_state_attributes["ContactState"])
            if self._proxy_type in NON_INVERTED_CONTACT_PROXIES:
                return contact_state
            return not contact_state
        _LOGGER.warning(
            "ContactState not found in extra_state_attributes: %s",
            str(self._extra_state_attributes),
        )

        return False

    @cached_property
    def device_class(self) -> BinarySensorDeviceClass:
        """Return the class of this device, from component DEVICE_CLASSES."""
        return self._device_class

    @cached_property
    def device_info(self):  # type: ignore[override]
        """Return info of parent Control4 device of entity."""
        # In Control4, binary sensors are not attached to a parent device.
        # Rather, they are attached to a room id.
        # Therefore, there is no device info for Home Assistant to use.
        return None


class Control4DynaliteTriggerBinarySensor(BinarySensorEntity):
    """Binary sensor for Dynalite triggers; state is set by the Dynalite TCP listener."""

    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

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
    ) -> None:
        """Initialize Dynalite trigger binary sensor."""
        self.entry_data = entry_data
        self.entry = entry
        self._attr_name = name
        self._attr_unique_id = f"{entry.entry_id}_dynalite_{idx}"
        self._idx = idx
        self._attr_is_on = False
        self._device_name = device_name
        self._device_manufacturer = device_manufacturer
        self._device_model = device_model
        self._device_id = device_id
        self._device_area = device_area
        self._reset_call = None

    async def async_added_to_hass(self) -> None:
        """Register this entity so the Dynalite listener can update it."""
        await super().async_added_to_hass()
        self.entry_data.setdefault("dynalite_entities", {})[self._idx] = self

    async def async_will_remove_from_hass(self) -> None:
        """Unregister from dynalite_entities."""
        if self._reset_call:
            self._reset_call()
            self._reset_call = None
        self.entry_data.get("dynalite_entities", {}).pop(self._idx, None)
        await super().async_will_remove_from_hass()

    def set_triggered(self) -> None:
        """Set state to on (triggered) and schedule reset to off after TRIGGER_RESET_SEC."""
        _LOGGER.debug(
            "Dynalite binary sensor triggered: item_id=%s entity_id=%s",
            self._idx,
            getattr(self, "entity_id", None),
        )
        if self._reset_call:
            self._reset_call()
            self._reset_call = None
        self._attr_is_on = True
        self.hass.add_job(self.async_write_ha_state)
        self._reset_call = async_call_later(
            self.hass,
            TRIGGER_RESET_SEC,
            self._async_reset_off,
        )

    async def _async_reset_off(self, _now: datetime) -> None:
        """Reset state to off on the event loop (async_call_later may not run sync callbacks on loop)."""
        self._reset_call = None
        self._attr_is_on = False
        self.async_write_ha_state()

    @property
    def device_info(self):
        """Return device info linking to the Control4 controller."""
        return {
            "identifiers": {(DOMAIN, self.entry_data[CONF_CONTROLLER_UNIQUE_ID])},
            "name": self._device_name or "Control4",
            "manufacturer": self._device_manufacturer or "Control4",
            "model": self._device_model,
        }
