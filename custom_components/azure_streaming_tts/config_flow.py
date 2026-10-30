"""UI setup, voice settings, and reconfiguration for Azure Streaming TTS."""

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_API_KEY, CONF_REGION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .const import (
    CONF_CONTOUR,
    CONF_GENDER,
    CONF_LANGUAGE,
    CONF_PITCH,
    CONF_RATE,
    CONF_SHORT_NAME,
    CONF_VOICE,
    CONF_VOLUME,
    DEFAULT_CONTOUR,
    DEFAULT_PITCH,
    DEFAULT_RATE,
    DEFAULT_REGION,
    DEFAULT_VOICE,
    DEFAULT_VOLUME,
    DOMAIN,
)
from .speech import AzureAuthError, AzureSpeechError, AzureVoice, async_list_voices

PERCENT = vol.All(vol.Coerce(int), vol.Range(min=-100, max=100))


def _connection_schema(region: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_API_KEY): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            vol.Required(CONF_REGION, default=region): str,
        }
    )


def _voice_schema(voices: list[AzureVoice], current: Mapping[str, Any]) -> vol.Schema:
    options = [
        SelectOptionDict(
            value=voice.short_name,
            label=f"{voice.display_name} ({voice.locale}, {voice.gender})",
        )
        for voice in sorted(voices, key=lambda voice: voice.short_name)
    ]
    return vol.Schema(
        {
            vol.Required(
                CONF_SHORT_NAME, default=current.get(CONF_SHORT_NAME, DEFAULT_VOICE)
            ): SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
            ),
            vol.Required(CONF_PITCH, default=current.get(CONF_PITCH, DEFAULT_PITCH)): str,
            vol.Required(CONF_RATE, default=current.get(CONF_RATE, DEFAULT_RATE)): PERCENT,
            vol.Required(
                CONF_VOLUME, default=current.get(CONF_VOLUME, DEFAULT_VOLUME)
            ): PERCENT,
            vol.Optional(
                CONF_CONTOUR, default=current.get(CONF_CONTOUR, DEFAULT_CONTOUR)
            ): str,
        }
    )


def _voice_options(voices: list[AzureVoice], user_input: dict[str, Any]) -> dict[str, Any]:
    """Expand the chosen voice into the settings Azure needs."""
    chosen = next(v for v in voices if v.short_name == user_input[CONF_SHORT_NAME])
    return {
        **user_input,
        CONF_CONTOUR: user_input.get(CONF_CONTOUR, DEFAULT_CONTOUR),
        CONF_LANGUAGE: chosen.locale.lower(),
        CONF_GENDER: chosen.gender,
        CONF_VOICE: chosen.voice,
    }


async def _async_try_list_voices(
    hass: HomeAssistant, api_key: str, region: str, errors: dict[str, str]
) -> list[AzureVoice] | None:
    """Return the voices, or record why they could not be read."""
    try:
        return await async_list_voices(async_get_clientsession(hass), api_key, region)
    except AzureAuthError:
        errors[CONF_API_KEY] = "invalid_auth"
    except AzureSpeechError:
        errors["base"] = "cannot_connect"
    return None


class AzureStreamingTtsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Connect to Azure, then pick a voice from what the account offers."""

    VERSION = 1

    def __init__(self) -> None:
        """Start with nothing validated."""
        self._connection: dict[str, Any] = {}
        self._voices: list[AzureVoice] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "AzureStreamingTtsOptionsFlow":
        """Let the voice settings change without re-entering the key."""
        return AzureStreamingTtsOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the key and region."""
        errors: dict[str, str] = {}
        if user_input is not None:
            voices = await _async_try_list_voices(
                self.hass, user_input[CONF_API_KEY], user_input[CONF_REGION], errors
            )
            if voices is not None:
                self._connection = user_input
                self._voices = voices
                return await self.async_step_voice()
        return self.async_show_form(
            step_id="user", data_schema=_connection_schema(DEFAULT_REGION), errors=errors
        )

    async def async_step_voice(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the voice and how it should sound."""
        if user_input is None:
            return self.async_show_form(
                step_id="voice", data_schema=_voice_schema(self._voices, {})
            )
        options = _voice_options(self._voices, user_input)
        return self.async_create_entry(
            title=f"Azure {options[CONF_VOICE]}", data=self._connection, options=options
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace the key or region of an existing entry."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            voices = await _async_try_list_voices(
                self.hass, user_input[CONF_API_KEY], user_input[CONF_REGION], errors
            )
            if voices is not None:
                if entry.options[CONF_SHORT_NAME] in {v.short_name for v in voices}:
                    return self.async_update_reload_and_abort(entry, data=user_input)
                errors[CONF_REGION] = "voice_not_in_region"
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_connection_schema(entry.data[CONF_REGION]),
            errors=errors,
        )


class AzureStreamingTtsOptionsFlow(OptionsFlowWithReload):
    """Change the voice settings of an existing entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the same voice form, filled in with the current values."""
        errors: dict[str, str] = {}
        voices = await _async_try_list_voices(
            self.hass,
            self.config_entry.data[CONF_API_KEY],
            self.config_entry.data[CONF_REGION],
            errors,
        )
        if voices is None:
            return self.async_abort(reason=errors.get("base", "invalid_auth"))
        if user_input is not None:
            return self.async_create_entry(data=_voice_options(voices, user_input))
        return self.async_show_form(
            step_id="init", data_schema=_voice_schema(voices, self.config_entry.options)
        )
