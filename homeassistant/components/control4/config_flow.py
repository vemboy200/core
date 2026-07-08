"""Config flow for Control4 integration."""
from __future__ import annotations

import asyncio
import json
import logging
from asyncio import TimeoutError as asyncioTimeoutError
from typing import Any

from aiohttp import web
from aiohttp.client_exceptions import ClientError
from pyControl4.account import C4Account
from pyControl4.director import C4Director
from pyControl4.error_handling import NotFound, Unauthorized
import voluptuous as vol

from homeassistant import config_entries, exceptions
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    aiohttp_client,
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    selector,
)
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.network import get_url

from .const import (
    CONF_ALARM_ARM_STATES,
    CONF_ALARM_AWAY_MODE,
    CONF_ALARM_CUSTOM_BYPASS_MODE,
    CONF_ALARM_HOME_MODE,
    CONF_ALARM_NIGHT_MODE,
    CONF_ALARM_VACATION_MODE,
    CONF_CONTROLLER_UNIQUE_ID,
    CONF_DIRECTOR_ALL_ITEMS,
    CONF_DYNALITE_ENABLED,
    CONF_DYNALITE_HOST,
    CONF_DYNALITE_PARSE_LAYOUT,
    CONF_DYNALITE_PORT,
    CONF_ENTITY_PREPEND_PARENT_NAME,
    DEFAULT_DYNALITE_PARSE_LAYOUT,
    DYNALITE_PARSE_LAYOUT_BYTES_2_3,
    DYNALITE_PARSE_LAYOUT_DYNET,
    CONTROL4_ENTITY_TYPE,
    DEFAULT_ALARM_AWAY_MODE,
    DEFAULT_ALARM_CUSTOM_BYPASS_MODE,
    DEFAULT_ALARM_HOME_MODE,
    DEFAULT_ALARM_NIGHT_MODE,
    DEFAULT_ALARM_VACATION_MODE,
    DEFAULT_ENTITY_PREPEND_PARENT_NAME,
    DEFAULT_DYNALITE_PORT,
    DOMAIN,
)
from .director_utils import (
    director_get_entry_variables,
    director_get_item_properties,
    director_has_dynalite_triggers,
)
from .location_floor import (
    LOCATION_FLOOR_FEATURES_AVAILABLE,
    _format_table,
    async_apply_area_floor_to_ha,
    async_build_location_floor_table,
    format_table_markdown,
)

_LOGGER = logging.getLogger(__name__)


async def build_control4_export_payload(
    hass: HomeAssistant, entry_id: str
) -> dict[str, Any] | None:
    """Build export payload (meta + items with variables and properties) for the given config entry."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if not entry:
        return None
    entry_data = getattr(entry, "runtime_data", None)
    if not entry_data:
        return None
    data = entry_data.get(CONF_DIRECTOR_ALL_ITEMS)
    if not data:
        return None

    export_list = [dict(item) for item in data]
    device_indices = [
        i
        for i, item in enumerate(export_list)
        if item.get("type") == CONTROL4_ENTITY_TYPE and item.get("id")
    ]

    async def fetch_vars(index: int) -> tuple[int, dict[str, Any]]:
        item = export_list[index]
        item_id = item["id"]
        try:
            variables = await director_get_entry_variables(hass, entry, item_id)
            return (index, variables)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Export: failed to get variables for item %s: %s", item_id, err)
            return (index, {"_error": str(err)})

    batch_size = 20
    for start in range(0, len(device_indices), batch_size):
        batch = device_indices[start : start + batch_size]
        results = await asyncio.gather(*(fetch_vars(i) for i in batch))
        for index, variables in results:
            export_list[index]["variables"] = variables

    # Items that should get Director properties (e.g. area/channel for Dynalite)
    def item_wants_properties(item: dict[str, Any]) -> bool:
        if item.get("proxy") == "dynalite_trigger":
            return True
        links = item.get("links") or []
        return any(
            "/properties" in (link.get("href") or "")
            for link in links
            if isinstance(link, dict)
        )

    properties_indices = [
        i
        for i, item in enumerate(export_list)
        if item.get("id") is not None and item_wants_properties(item)
    ]

    async def fetch_props(index: int) -> tuple[int, dict[str, Any] | None]:
        item = export_list[index]
        item_id = item["id"]
        props = await director_get_item_properties(hass, entry, item_id)
        return (index, props)

    for start in range(0, len(properties_indices), batch_size):
        batch = properties_indices[start : start + batch_size]
        results = await asyncio.gather(*(fetch_props(i) for i in batch))
        for index, props in results:
            export_list[index]["properties"] = props

    dev_reg = dr.async_get(hass)
    ha_device_count = sum(
        1 for d in dev_reg.devices.values() if entry.entry_id in d.config_entries
    )
    entity_reg = er.async_get(hass)
    ha_entities: dict[str, int] = {}
    for entity in entity_reg.entities.values():
        if entity.config_entry_id != entry.entry_id:
            continue
        platform = entity.platform or "unknown"
        ha_entities[platform] = ha_entities.get(platform, 0) + 1

    return {
        "meta": {"ha_device_count": ha_device_count, "ha_entities": ha_entities},
        "items": export_list,
    }


class Control4ExportView(HomeAssistantView):
    """HTTP view to serve director_all_items as JSON for browser download (Save As)."""

    requires_auth = False
    url = "/api/control4/export"
    name = "api:control4:export"

    async def get(self, request: web.Request) -> web.Response:
        """Serve pretty-printed JSON; uses build_control4_export_payload on demand."""
        hass = request.app["hass"]
        entry_id = request.query.get("entry_id")
        if not entry_id:
            return web.Response(
                status=400,
                text="entry_id query parameter required",
                content_type="text/plain",
            )
        payload = await build_control4_export_payload(hass, entry_id)
        if not payload:
            # 503 so the link is valid but data wasn't ready (e.g. integration still loading)
            msg = (
                "Entry not found or export data not available. "
                "If you just reloaded, wait for the Control4 integration to finish loading and try again."
            )
            _LOGGER.warning("Export failed for entry_id=%s: %s", entry_id, msg)
            return web.Response(
                status=503,
                text=msg,
                content_type="text/plain",
            )
        try:
            body = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as err:
            _LOGGER.exception("Export serialize error: %s", err)
            return web.Response(status=500, text=f"Serialize error: {err}")
        return web.Response(
            body=body,
            content_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="control4_export.json"'},
        )

# Workaround: custom control4 shares domain with core HA integration; bundled
# frontend translations win for standard keys (host, unknown, …) and show
# unresolved [%key:...]. Use human-readable keys core does not define.
# Revert to CONF_HOST / standard error keys when fixed upstream:
# https://github.com/home-assistant/frontend/issues/52600
_FORM_LABEL_HOST = "IP address"
_FORM_LABEL_USERNAME = "Username"
_FORM_LABEL_PASSWORD = "Password"

# Same issue for config.error / config.abort lookup keys (frontend#52600).
_ERROR_CANNOT_CONNECT = "Failed to connect"
_ERROR_INVALID_AUTH = "Invalid authentication"
_ERROR_UNKNOWN = "Unknown error"
_ERROR_REAUTH_FAILED = "Reauthentication failed"
_ABORT_ALREADY_CONFIGURED = "Device is already configured"
_ABORT_REAUTH_SUCCESSFUL = "Reauthentication was successful"

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(_FORM_LABEL_HOST): str,
        vol.Required(_FORM_LABEL_USERNAME): str,
        vol.Required(_FORM_LABEL_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(
                type=selector.TextSelectorType.PASSWORD,
                autocomplete="current-password",
            )
        ),
    }
)


def _credentials_from_form(user_input: dict[str, Any]) -> dict[str, str]:
    """Map config-flow form labels to config entry credential keys."""
    return {
        CONF_HOST: user_input[_FORM_LABEL_HOST],
        CONF_USERNAME: user_input[_FORM_LABEL_USERNAME],
        CONF_PASSWORD: user_input[_FORM_LABEL_PASSWORD],
    }


def _suggested_credentials(entry_data: dict[str, Any]) -> dict[str, str]:
    """Map stored config entry credentials to config-flow form labels."""
    return {
        _FORM_LABEL_HOST: entry_data[CONF_HOST],
        _FORM_LABEL_USERNAME: entry_data[CONF_USERNAME],
        _FORM_LABEL_PASSWORD: entry_data[CONF_PASSWORD],
    }


class Control4Validator:
    """Validates that config details can be used to authenticate and communicate with Control4."""

    def __init__(self, host, username, password, hass):
        """Initialize."""
        self.host = host
        self.username = username
        self.password = password
        self.controller_unique_id = None
        self.director_bearer_token = None
        self.hass = hass

    async def authenticate(self) -> bool:
        """Test if we can authenticate with the Control4 account API."""
        try:
            account_session = aiohttp_client.async_get_clientsession(self.hass)
            account = C4Account(self.username, self.password, account_session)
            # Authenticate with Control4 account
            await account.get_account_bearer_token()

            # Get controller name
            account_controllers = await account.get_account_controllers()
            self.controller_unique_id = account_controllers["controllerCommonName"]

            # Get bearer token to communicate with controller locally
            self.director_bearer_token = (
                await account.get_director_bearer_token(self.controller_unique_id)
            )["token"]
            return True
        except (Unauthorized, NotFound):
            return False
        except (ClientError, asyncioTimeoutError) as err:
            _LOGGER.debug("Control4 account API connection failed: %s", err)
            raise CannotConnect from err

    async def connect_to_director(self) -> bool:
        """Test if we can connect to the local Control4 Director."""
        if self.director_bearer_token is None:
            _LOGGER.error("Director bearer token is not set")
            return False
        try:
            director_session = aiohttp_client.async_get_clientsession(
                self.hass, verify_ssl=False
            )
            director = C4Director(
                self.host, self.director_bearer_token, director_session
            )
            await director.get_all_item_info()
            return True
        except (Unauthorized, ClientError, asyncioTimeoutError):
            _LOGGER.error("Failed to connect to the Control4 controller")
            return False


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Control4."""

    VERSION = 1

    async def _validate_input(self, credentials: dict[str, str]):
        errors = {}
        hub = Control4Validator(
            credentials[CONF_HOST],
            credentials[CONF_USERNAME],
            credentials[CONF_PASSWORD],
            self.hass,
        )
        try:
            if not await hub.authenticate():
                raise InvalidAuth
            if not await hub.connect_to_director():
                raise CannotConnect
        except InvalidAuth:
            errors["base"] = _ERROR_INVALID_AUTH
        except CannotConnect:
            errors["base"] = _ERROR_CANNOT_CONNECT
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Unexpected exception")
            errors["base"] = _ERROR_UNKNOWN

        return errors, hub.controller_unique_id

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            credentials = _credentials_from_form(user_input)
            errors, controller_unique_id = await self._validate_input(credentials)
            if not errors:
                assert controller_unique_id is not None
                mac = (controller_unique_id.split("_", 3))[2]
                formatted_mac = format_mac(mac)
                data = {
                    **credentials,
                    CONF_CONTROLLER_UNIQUE_ID: controller_unique_id,
                }
                await self.async_set_unique_id(formatted_mac)
                self._abort_if_unique_id_configured(error=_ABORT_ALREADY_CONFIGURED)
                return self.async_create_entry(
                    title=controller_unique_id,
                    data=data,
                )

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )

    async def async_step_user_reauth(self, user_input=None):
        """Handle a reauthentication request."""
        errors = {}
        if user_input is not None:
            credentials = _credentials_from_form(user_input)
            errors, controller_unique_id = await self._validate_input(credentials)
            if not errors:
                assert controller_unique_id is not None
                mac = (controller_unique_id.split("_", 3))[2]
                formatted_mac = format_mac(mac)
                data = {
                    **credentials,
                    CONF_CONTROLLER_UNIQUE_ID: controller_unique_id,
                }
                _LOGGER.debug("Reauthentication occurring")
                existing_entry = await self.async_set_unique_id(formatted_mac)
                if existing_entry is None:
                    errors["base"] = _ERROR_REAUTH_FAILED
                else:
                    self.hass.config_entries.async_update_entry(existing_entry, data=data)
                    await self.hass.config_entries.async_reload(existing_entry.entry_id)
                    return self.async_abort(reason=_ABORT_REAUTH_SUCCESSFUL)

        entry = self._get_reauth_entry()
        return self.async_show_form(
            step_id="user_reauth",
            data_schema=self.add_suggested_values_to_schema(
                DATA_SCHEMA, _suggested_credentials(entry.data)
            ),
            errors=errors,
        )

    async def async_step_reauth(self, user_input=None):
        """Perform reauth upon an API authentication error."""
        return await self.async_step_user_reauth()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return OptionsFlowHandler(config_entry)


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle a option flow for Control4."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        # Do not assign to self.config_entry; it's a read-only property in HA.
        self._config_entry = config_entry

    def _entry_data_ready(self):
        """Return True if integration entry data is loaded (table, apply, export, JSON download)."""
        entry_data = getattr(self._config_entry, "runtime_data", None)
        return bool(entry_data and entry_data.get(CONF_DIRECTOR_ALL_ITEMS))

    @staticmethod
    def _options_menu_choices() -> dict[str, str]:
        """Build init-step menu; area/floor/file export require floor registry (HA 2024.3+)."""
        menu = {
            "configure": "Configure options (scan interval, alarm modes, Dynalite)",
            "download": "Download JSON file (browser)",
        }
        if LOCATION_FLOOR_FEATURES_AVAILABLE:
            menu["table"] = "Show c4 device area/floor"
            menu["apply"] = "Apply c4 device area/floor to HA"
            menu["export_file"] = (
                "Write director export JSON to configuration folder"
            )
        return menu

    async def _notify_and_close(self, title: str, message: str, notification_id: str):
        """Send a persistent notification and close the options flow."""
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": title,
                "message": message,
                "notification_id": notification_id,
            },
        )
        return self.async_create_entry(title="", data=self._config_entry.options)

    async def _handle_show_table(self):
        """Build area/floor table, send notification, and close flow."""
        try:
            rows = await async_build_location_floor_table(
                self.hass, self._config_entry
            )
            full_table = _format_table(rows, max_rows=999)
            notification_table = format_table_markdown(rows, max_rows=150)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.exception("Failed to build area/floor table: %s", err)
            full_table = f"(Error: {err})"
            notification_table = f"_Error: {err}_"
        _LOGGER.info(
            "Control4 area/floor mapping (full table):\n%s",
            full_table,
        )
        return await self._notify_and_close(
            "Control4 area/floor mapping",
            notification_table,
            "control4_area_floor_table",
        )

    def _format_apply_message(self, summary: dict) -> str:
        """Build the notification message from apply summary."""
        created_f = summary.get("created_floors") or []
        created_a = summary.get("created_areas") or []
        updated = summary.get("updated_devices") or 0
        mismatched = summary.get("mismatched_after_apply", 0)
        errs = summary.get("errors") or []
        skipped = summary.get("skipped_no_area") or []
        skipped_room = summary.get("skipped_no_c4_room") or []
        msg = (
            f"- **Floors created:** {len(created_f)}\n"
            f"- **Areas created:** {len(created_a)}\n"
            f"- **Devices assigned:** {updated}\n"
            f"- **C4 devices with room/floor different from HA:** {mismatched}\n"
        )
        if skipped_room:
            msg += (
                f"\n**Skipped (C4 has no room name, no HA area):** "
                f"{len(skipped_room)} entities (IDs: {', '.join(str(x) for x in skipped_room[:30])}"
            )
            if len(skipped_room) > 30:
                msg += ", …"
            msg += ")\n"
        if skipped:
            msg += f"\n**Skipped (no area for location):** {len(skipped)} devices\n"
            for row_id, fl, rm in skipped[:20]:
                msg += f"- ID {row_id}: floor={fl!r}, room={rm!r}\n"
            if len(skipped) > 20:
                msg += f"- ... and {len(skipped) - 20} more\n"
        if errs:
            msg += "\n**Errors:**\n" + "\n".join(f"- {e}" for e in errs)
        return msg

    async def _handle_apply(self):
        """Run apply area/floor to HA, send result notification, and close flow."""
        try:
            summary = await async_apply_area_floor_to_ha(
                self.hass, self._config_entry
            )
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.exception("Failed to apply area/floor to HA: %s", err)
            summary = {
                "errors": [str(err)],
                "created_floors": [],
                "created_areas": [],
                "updated_devices": 0,
                "skipped_no_area": [],
                "skipped_no_c4_room": [],
                "mismatched_after_apply": 0,
            }
        msg = self._format_apply_message(summary)
        return await self._notify_and_close(
            "Control4 apply area/floor",
            msg,
            "control4_apply_area_floor",
        )

    async def _handle_write_export(self):
        """Write director export JSON under the HA configuration directory."""
        entry_id = self._config_entry.entry_id
        payload = await build_control4_export_payload(self.hass, entry_id)
        if not payload:
            return await self._notify_and_close(
                "Control4 export",
                "Export data is not available. Wait for the Control4 integration to finish loading, then try again.",
                "control4_export_json",
            )
        filename = f"control4_director_export_{entry_id}.json"
        path = self.hass.config.path(filename)

        def _write() -> None:
            with open(path, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, indent=2, ensure_ascii=False, default=str)

        try:
            await self.hass.async_add_executor_job(_write)
        except OSError as err:
            _LOGGER.exception("Failed to write Control4 export: %s", err)
            return await self._notify_and_close(
                "Control4 export",
                f"Could not write export file: {err}",
                "control4_export_json",
            )
        return await self._notify_and_close(
            "Control4 export",
            f"Wrote director export JSON to:\n\n`{path}`",
            "control4_export_json",
        )

    async def _handle_download(self):
        """Notify with a link that triggers browser Save As when opened."""
        base = get_url(self.hass)
        path = f"/api/control4/export?entry_id={self._config_entry.entry_id}"
        if base:
            url = base.rstrip("/") + path
            msg = f"Open this link in your browser to download the JSON file (Save As):\n\n{url}"
        else:
            msg = (
                "Could not determine HA URL. Set it in Settings → Home Assistant URL (Local Network), "
                f"or open this path in your browser (prepend your HA address):\n\n{path}"
            )
        return await self._notify_and_close(
            "Control4 download JSON",
            msg,
            "control4_download_json",
        )

    async def async_step_init(self, user_input=None):
        """Handle options flow: menu (configure, download, optional area/floor)."""
        choices = self._options_menu_choices()
        if user_input is not None:
            choice = user_input.get("Settings")
            if choice == "download":
                if not self._entry_data_ready():
                    return await self._notify_and_close(
                        "Control4 download JSON",
                        "Integration data is not ready. Please try again after the integration has finished loading.",
                        "control4_download_json",
                    )
                return await self._handle_download()
            if choice == "table" and LOCATION_FLOOR_FEATURES_AVAILABLE:
                if not self._entry_data_ready():
                    return await self._notify_and_close(
                        "Control4 area/floor mapping",
                        "Integration data is not ready. Please try again after the integration has finished loading.",
                        "control4_area_floor_table",
                    )
                return await self._handle_show_table()
            if choice == "apply" and LOCATION_FLOOR_FEATURES_AVAILABLE:
                if not self._entry_data_ready():
                    return await self._notify_and_close(
                        "Control4 apply area/floor",
                        "Integration data is not ready. Please try again after the integration has finished loading.",
                        "control4_apply_area_floor",
                    )
                return await self._handle_apply()
            if choice == "export_file" and LOCATION_FLOOR_FEATURES_AVAILABLE:
                if not self._entry_data_ready():
                    return await self._notify_and_close(
                        "Control4 export",
                        "Integration data is not ready. Please try again after the integration has finished loading.",
                        "control4_export_json",
                    )
                return await self._handle_write_export()
            return await self.async_step_configure()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("Settings", default="configure"): vol.In(choices),
                }
            ),
            description_placeholders={"heading": "Control4 settings"},
        )

    async def async_step_configure(self, user_input=None):
        """Handle the configure-options form (scan interval, alarm modes, Dynalite TCP, entity naming)."""
        if user_input is not None:
            _LOGGER.debug(user_input)
            entry_data_submit = getattr(self._config_entry, "runtime_data", None) or {}
            has_dynalite = director_has_dynalite_triggers(entry_data_submit)
            enabled = bool(user_input.get(CONF_DYNALITE_ENABLED, False)) and has_dynalite
            if not enabled:
                data = {
                    **user_input,
                    CONF_DYNALITE_ENABLED: False,
                    CONF_DYNALITE_HOST: "",
                    CONF_DYNALITE_PORT: DEFAULT_DYNALITE_PORT,
                    CONF_DYNALITE_PARSE_LAYOUT: self._config_entry.options.get(
                        CONF_DYNALITE_PARSE_LAYOUT, DEFAULT_DYNALITE_PARSE_LAYOUT
                    ),
                }
                return self.async_create_entry(title="", data=data)
            self._configure_base = user_input
            return await self.async_step_configure_dynalite_tcp()

        # TODO: figure out how to accept empty strings to disable modes
        # TODO: figure out how to only show alarm options if a alarm_control_panel entity exists
        self.entry_data = self._config_entry.runtime_data

        # Minimal approach: use existing cached arm states only
        arm_state_choices = set(self.entry_data.get(CONF_ALARM_ARM_STATES, [])) or {
            DEFAULT_ALARM_AWAY_MODE
        }
        # Determine if a security panel is effectively present (has real arm states)
        has_security = any(
            x.strip() and x.strip() != DEFAULT_ALARM_AWAY_MODE for x in arm_state_choices
        )

        prepend_default = self._config_entry.options.get(
            CONF_ENTITY_PREPEND_PARENT_NAME, DEFAULT_ENTITY_PREPEND_PARENT_NAME
        )

        # Base schema: alarm options only if we have a panel
        schema_dict = {}
        if has_security:
            schema_dict.update(
                {
                    vol.Optional(
                        CONF_ALARM_AWAY_MODE,
                        default=self._config_entry.options.get(
                            CONF_ALARM_AWAY_MODE, DEFAULT_ALARM_AWAY_MODE
                        ),
                    ): vol.In(sorted(arm_state_choices)),
                    vol.Optional(
                        CONF_ALARM_HOME_MODE,
                        default=self._config_entry.options.get(
                            CONF_ALARM_HOME_MODE, DEFAULT_ALARM_HOME_MODE
                        ),
                    ): vol.In(sorted(arm_state_choices)),
                    vol.Optional(
                        CONF_ALARM_NIGHT_MODE,
                        default=self._config_entry.options.get(
                            CONF_ALARM_NIGHT_MODE, DEFAULT_ALARM_NIGHT_MODE
                        ),
                    ): vol.In(sorted(arm_state_choices)),
                    vol.Optional(
                        CONF_ALARM_CUSTOM_BYPASS_MODE,
                        default=self._config_entry.options.get(
                            CONF_ALARM_CUSTOM_BYPASS_MODE, DEFAULT_ALARM_CUSTOM_BYPASS_MODE
                        ),
                    ): vol.In(sorted(arm_state_choices)),
                    vol.Optional(
                        CONF_ALARM_VACATION_MODE,
                        default=self._config_entry.options.get(
                            CONF_ALARM_VACATION_MODE, DEFAULT_ALARM_VACATION_MODE
                        ),
                    ): vol.In(sorted(arm_state_choices)),
                }
            )

        schema_dict[
            vol.Optional(
                CONF_ENTITY_PREPEND_PARENT_NAME,
                default=prepend_default,
            )
        ] = selector.BooleanSelector()

        # Dynalite: TCP only; show enable toggle only when project has dynalite_trigger devices
        has_dynalite = director_has_dynalite_triggers(self.entry_data)
        desc: dict[str, str] = {}
        if has_dynalite:
            schema_dict.update(
                {
                    vol.Optional(
                        CONF_DYNALITE_ENABLED,
                        default=self._config_entry.options.get(
                            CONF_DYNALITE_ENABLED, False
                        ),
                        description="Enable Dynalite gateway listener (TCP)",
                    ): bool,
                }
            )
            desc[CONF_DYNALITE_ENABLED] = (
                "Listen to the Dynalite gateway via TCP for trigger events (binary sensors)."
            )
        schema = vol.Schema(schema_dict, required=False)
        try:
            return self.async_show_form(
                step_id="configure",
                data_schema=schema,
                data_description=desc,
            )
        except TypeError:
            return self.async_show_form(step_id="configure", data_schema=schema)

    async def async_step_configure_dynalite_tcp(self, user_input=None):
        """Collect TCP host/port and frame layout for Dynalite gateway."""
        if user_input is not None:
            merged = {
                **self._configure_base,
                CONF_DYNALITE_HOST: (user_input.get(CONF_DYNALITE_HOST) or "").strip(),
                CONF_DYNALITE_PORT: user_input.get(
                    CONF_DYNALITE_PORT, DEFAULT_DYNALITE_PORT
                ),
                CONF_DYNALITE_PARSE_LAYOUT: user_input.get(
                    CONF_DYNALITE_PARSE_LAYOUT, DEFAULT_DYNALITE_PARSE_LAYOUT
                ),
            }
            return self.async_create_entry(title="", data=merged)
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_DYNALITE_HOST,
                    default=self._config_entry.options.get(CONF_DYNALITE_HOST, ""),
                    description="Dynalite gateway IP",
                ): str,
                vol.Optional(
                    CONF_DYNALITE_PORT,
                    default=self._config_entry.options.get(
                        CONF_DYNALITE_PORT, DEFAULT_DYNALITE_PORT
                    ),
                    description="Dynalite gateway port",
                ): cv.port,
                vol.Optional(
                    CONF_DYNALITE_PARSE_LAYOUT,
                    default=self._config_entry.options.get(
                        CONF_DYNALITE_PARSE_LAYOUT, DEFAULT_DYNALITE_PARSE_LAYOUT
                    ),
                    description="TCP frame layout",
                ): vol.In(
                    {
                        DYNALITE_PARSE_LAYOUT_DYNET: "DyNet (standard bus)",
                        DYNALITE_PARSE_LAYOUT_BYTES_2_3: "Bytes 2–3 (legacy)",
                    }
                ),
            }
        )
        return self.async_show_form(
            step_id="configure_dynalite_tcp",
            data_schema=schema,
        )


class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(exceptions.HomeAssistantError):
    """Error to indicate there is invalid auth."""
