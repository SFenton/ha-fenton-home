"""Config flow: paste the APNs auth key (.p8), its Key ID and the Team ID, checked with Apple before saving."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .apns import APNsClient, APNsError
from .const import (
    CONF_BUNDLE_ID,
    CONF_CRITICAL_ALERTS,
    CONF_KEY,
    CONF_KEY_ID,
    CONF_LIVE_DEVICES,
    CONF_LIVE_JOBS,
    CONF_LIVE_JOBS_ENABLED,
    CONF_PERSON_NOTIFY,
    CONF_PUSH_BASE_URL,
    CONF_SWEEP_MODE,
    CONF_TEAM_ID,
    DEFAULT_BUNDLE_ID,
    DEFAULT_SWEEP_MODE,
    DOMAIN,
    LEGACY_DOMAIN,
    SWEEP_MODES,
)
from .entities import DEFAULT_PERSON_NOTIFY, JOB_IDS, STYLES
from .legacy import async_flag_still_installed, async_migrate_store, legacy_loaded
from .targets import live_devices

KEY_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, multiline=False))


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    key = user_input[CONF_KEY].strip()
    # Pasted into a single-line field, the PEM's line breaks may arrive as spaces.
    if "\n" not in key and "-----BEGIN PRIVATE KEY-----" in key:
        body = key.replace("-----BEGIN PRIVATE KEY-----", "").replace("-----END PRIVATE KEY-----", "").split()
        key = "-----BEGIN PRIVATE KEY-----\n" + "\n".join(body) + "\n-----END PRIVATE KEY-----"
    return {CONF_KEY: key, CONF_KEY_ID: user_input[CONF_KEY_ID].strip().upper(),
            CONF_TEAM_ID: user_input[CONF_TEAM_ID].strip().upper(),
            CONF_BUNDLE_ID: user_input[CONF_BUNDLE_ID].strip()}


class FentonPushConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up Apple Push for the Fenton Home bundle."""

    VERSION = 1

    async def _verify(self, data: dict[str, Any]) -> str | None:
        """Return an error code, or None when Apple accepts the key for this bundle."""
        from . import credentials, http_client  # noqa: PLC0415

        try:
            client = APNsClient(credentials(data), http_client(self.hass))
        except ValueError:
            return "invalid_key"
        try:
            await client.verify()
        except APNsError as err:
            if err.status == 0:
                return "cannot_connect"
            if err.reason in ("TopicDisallowed", "BadTopic", "DeviceTokenNotForTopic"):
                return "bundle_rejected"
            if err.bad_credentials or err.status == 403:
                return "key_rejected"
            return "cannot_connect"
        return None

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Copy a ``fenton_push`` entry verbatim (``legacy.py``): same data, options and unique id (the bundle id).

        The values are not checked with Apple again, and never logged.
        """
        if legacy_loaded(self.hass):
            async_flag_still_installed(self.hass)
            return self.async_abort(reason="legacy_installed")
        data = dict(import_data["data"])
        await self.async_set_unique_id(import_data.get("unique_id") or data.get(CONF_BUNDLE_ID))
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=import_data.get("title") or f"{data.get(CONF_BUNDLE_ID)} (key {data.get(CONF_KEY_ID)})",
                                       data=data, options=dict(import_data.get("options") or {}))

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the key and identifiers."""
        errors: dict[str, str] = {}
        if (old := self.hass.config_entries.async_entries(LEGACY_DOMAIN)) and user_input is None:
            if legacy_loaded(self.hass):
                async_flag_still_installed(self.hass)
                return self.async_abort(reason="legacy_installed")
            # Adding the integration on a server that still has Fenton Home Push: take its key, ask for nothing.
            await async_migrate_store(self.hass)
            # The old entry is removed by ``async_setup_entry`` once this one exists.
            return await self.async_step_import({"data": dict(old[0].data), "options": dict(old[0].options),
                                                 "unique_id": old[0].unique_id, "title": old[0].title})
        if user_input is not None:
            data = _clean(user_input)
            await self.async_set_unique_id(data[CONF_BUNDLE_ID])
            self._abort_if_unique_id_configured()
            if (error := await self._verify(data)) is None:
                return self.async_create_entry(title=f"{data[CONF_BUNDLE_ID]} (key {data[CONF_KEY_ID]})", data=data)
            errors["base"] = error
        schema = vol.Schema({
            vol.Required(CONF_KEY): KEY_SELECTOR,
            vol.Required(CONF_KEY_ID): str,
            vol.Required(CONF_TEAM_ID): str,
            vol.Required(CONF_BUNDLE_ID, default=DEFAULT_BUNDLE_ID): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> FentonPushOptionsFlow:
        return FentonPushOptionsFlow()


class FentonPushOptionsFlow(OptionsFlow):
    """Critical alerts (once Apple grants the entitlement), an explicit push base URL and the job cards."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data={
                CONF_CRITICAL_ALERTS: bool(user_input.get(CONF_CRITICAL_ALERTS, False)),
                CONF_PUSH_BASE_URL: (user_input.get(CONF_PUSH_BASE_URL) or "").strip(),
                CONF_LIVE_JOBS_ENABLED: bool(user_input.get(CONF_LIVE_JOBS_ENABLED, True)),
                CONF_LIVE_JOBS: [j for j in user_input.get(CONF_LIVE_JOBS, list(JOB_IDS)) if j in JOB_IDS],
                CONF_LIVE_DEVICES: list(user_input.get(CONF_LIVE_DEVICES) or []),
                CONF_PERSON_NOTIFY: (user_input.get(CONF_PERSON_NOTIFY) or DEFAULT_PERSON_NOTIFY).strip(),
                CONF_SWEEP_MODE: user_input.get(CONF_SWEEP_MODE) if user_input.get(CONF_SWEEP_MODE) in SWEEP_MODES
                else DEFAULT_SWEEP_MODE,
            })
        options = self.config_entry.options
        bundle = self.config_entry.data.get(CONF_BUNDLE_ID, DEFAULT_BUNDLE_ID)
        devices = {d.webhook_id: d.name for d in live_devices(self.hass, bundle)}
        for webhook_id in options.get(CONF_LIVE_DEVICES) or []:
            devices.setdefault(webhook_id, webhook_id)
        schema = vol.Schema({
            vol.Optional(CONF_CRITICAL_ALERTS, default=options.get(CONF_CRITICAL_ALERTS, False)): BooleanSelector(),
            vol.Optional(CONF_PUSH_BASE_URL, description={"suggested_value": options.get(CONF_PUSH_BASE_URL, "")}):
                TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            vol.Optional(CONF_LIVE_JOBS_ENABLED, default=options.get(CONF_LIVE_JOBS_ENABLED, True)): BooleanSelector(),
            vol.Optional(CONF_LIVE_JOBS, default=options.get(CONF_LIVE_JOBS, list(JOB_IDS))): SelectSelector(
                SelectSelectorConfig(options=[SelectOptionDict(value=job, label=STYLES[job].title) for job in JOB_IDS],
                                     multiple=True, mode=SelectSelectorMode.LIST)),
            vol.Optional(CONF_LIVE_DEVICES, default=options.get(CONF_LIVE_DEVICES, [])): SelectSelector(
                SelectSelectorConfig(options=[SelectOptionDict(value=k, label=v) for k, v in sorted(devices.items())],
                                     multiple=True, mode=SelectSelectorMode.LIST)),
            vol.Optional(CONF_PERSON_NOTIFY, default=options.get(CONF_PERSON_NOTIFY, DEFAULT_PERSON_NOTIFY)):
                TextSelector(),
            vol.Optional(CONF_SWEEP_MODE, default=options.get(CONF_SWEEP_MODE, DEFAULT_SWEEP_MODE)): SelectSelector(
                SelectSelectorConfig(options=list(SWEEP_MODES), translation_key=CONF_SWEEP_MODE,
                                     mode=SelectSelectorMode.DROPDOWN)),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
