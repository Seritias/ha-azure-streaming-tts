"""Streaming text-to-speech entity backed by Azure Speech."""

from collections.abc import Mapping
from typing import Any

from homeassistant.components.tts import (
    TextToSpeechEntity,
    TTSAudioRequest,
    TTSAudioResponse,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, CONF_REGION
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_CONTOUR,
    CONF_GENDER,
    CONF_LANGUAGE,
    CONF_PITCH,
    CONF_RATE,
    CONF_VOICE,
    CONF_VOLUME,
)
from .speech import AzureSpeechClient, VoiceSettings


def settings_from_config(config: Mapping[str, Any]) -> VoiceSettings:
    """Build voice settings the way core's microsoft integration does."""
    return VoiceSettings(
        language=config[CONF_LANGUAGE],
        gender=config[CONF_GENDER],
        voice=config[CONF_VOICE],
        rate=f"{config[CONF_RATE]}%",
        volume=f"{config[CONF_VOLUME]}%",
        pitch=config[CONF_PITCH],
        contour=config.get(CONF_CONTOUR, ""),
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the one TTS entity for this entry."""
    client = AzureSpeechClient(
        async_get_clientsession(hass),
        entry.data[CONF_API_KEY],
        entry.data[CONF_REGION],
        settings_from_config(entry.options),
    )
    async_add_entities([AzureStreamingTtsEntity(entry, client)])


class AzureStreamingTtsEntity(TextToSpeechEntity):
    """Speak one configured voice, streaming audio while text arrives."""

    def __init__(self, entry: ConfigEntry, client: AzureSpeechClient) -> None:
        """Bind the entity to its config entry."""
        self._client = client
        self._attr_name = f"Azure {entry.options[CONF_VOICE]}"
        self._attr_unique_id = entry.entry_id
        self._attr_default_language = entry.options[CONF_LANGUAGE]
        self._attr_supported_languages = [entry.options[CONF_LANGUAGE]]

    async def async_stream_tts_audio(self, request: TTSAudioRequest) -> TTSAudioResponse:
        """Return audio generated from the incoming text stream."""
        return TTSAudioResponse("mp3", self._client.async_stream_text(request.message_gen))
