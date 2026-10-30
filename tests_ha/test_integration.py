"""End-to-end behavior inside a real Home Assistant, with Azure faked at HTTP."""

import asyncio
import shutil

from homeassistant.components import ffmpeg, tts
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_API_KEY, CONF_REGION
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.azure_streaming_tts.const import DOMAIN

REGION = "germanywestcentral"
HOST = f"https://{REGION}.tts.speech.microsoft.com/cognitiveservices"
VOICES = [
    {"ShortName": "en-US-AshleyNeural", "DisplayName": "Ashley", "Locale": "en-US", "Gender": "Female"},
    {"ShortName": "en-GB-RyanNeural", "DisplayName": "Ryan", "Locale": "en-GB", "Gender": "Male"},
]
NEURO_FORM = {"short_name": "en-US-AshleyNeural", "pitch": "+25%", "rate": 0, "volume": 100, "contour": ""}
NEURO_OPTIONS = {**NEURO_FORM, "language": "en-us", "gender": "Female", "voice": "AshleyNeural"}


@pytest.fixture
def azure(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    aioclient_mock.get(f"{HOST}/voices/list", json=VOICES)
    aioclient_mock.post(f"{HOST}/v1", content=b"AUDIO")
    return aioclient_mock


@pytest.fixture
async def entry(hass: HomeAssistant, azure) -> MockConfigEntry:
    assert await async_setup_component(hass, "tts", {})
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Azure AshleyNeural",
        data={CONF_API_KEY: "secret", CONF_REGION: REGION},
        options=NEURO_OPTIONS,
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


async def test_setup_from_ui_picks_neuro_defaults(hass: HomeAssistant, azure) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "secret", CONF_REGION: REGION}
    )
    assert result["step_id"] == "voice"
    defaults = {str(key): key.default() for key in result["data_schema"].schema}
    assert defaults == NEURO_FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], NEURO_FORM)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == NEURO_OPTIONS
    assert result["data"] == {CONF_API_KEY: "secret", CONF_REGION: REGION}


@pytest.mark.parametrize(("status", "error"), [(401, {CONF_API_KEY: "invalid_auth"}), (500, {"base": "cannot_connect"})])
async def test_setup_reports_bad_key_and_outage(hass, aioclient_mock, status, error) -> None:
    aioclient_mock.get(f"{HOST}/voices/list", status=status)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "bad", CONF_REGION: REGION}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == error


async def test_entity_declares_streaming(hass: HomeAssistant, entry) -> None:
    assert entry.state is ConfigEntryState.LOADED
    engine = tts.async_resolve_engine(hass, "tts.azure_ashleyneural")
    assert engine == "tts.azure_ashleyneural"
    assert tts.get_engine_instance(hass, engine).async_supports_streaming_input() is True


async def test_full_tts_path_sends_neuro_ssml_per_sentence(hass: HomeAssistant, entry, azure) -> None:
    stream = tts.async_create_stream(hass, "tts.azure_ashleyneural", "en-us", {tts.ATTR_PREFERRED_FORMAT: "mp3"})
    assert stream.supports_streaming_input is True

    async def text():
        yield "This first sentence is long enough to go out alone. "
        yield "And the second sentence is long enough as well."

    stream.async_set_message_stream(text())
    audio = b"".join([chunk async for chunk in stream.async_stream_result()])
    assert audio == b"AUDIOAUDIO"
    bodies = [call[2] for call in azure.mock_calls if str(call[1]).endswith("/v1")]
    assert len(bodies) == 2
    for body in bodies:
        assert b"(en-US, AshleyNeural)" in body
        assert b'pitch="+25%"' in body
        assert b'volume="100%"' in body


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
async def test_real_mp3_sentences_convert_to_flac_without_losing_audio(hass, entry, aioclient_mock) -> None:
    """The Voice PE asks for FLAC, so joined MP3s must survive HA's ffmpeg step."""
    assert await async_setup_component(hass, ffmpeg.DOMAIN, {})

    async def run(*args: str, stdin: bytes | None = None) -> bytes:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await process.communicate(stdin)
        assert process.returncode == 0
        return output

    one_second_mp3 = await run(
        "ffmpeg", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-ar", "24000", "-ac", "1",
        "-b:a", "96k", "-f", "mp3", "-write_xing", "0", "-id3v2_version", "0", "pipe:1",
    )
    aioclient_mock.clear_requests()
    aioclient_mock.post(f"{HOST}/v1", content=one_second_mp3)
    stream = tts.async_create_stream(hass, "tts.azure_ashleyneural", "en-us", {tts.ATTR_PREFERRED_FORMAT: "flac"})

    async def text():
        yield "This first sentence is long enough to go out alone. "
        yield "And the second sentence is long enough as well. "
        yield "And here is a third sentence that is long enough too."

    stream.async_set_message_stream(text())
    flac = b"".join([chunk async for chunk in stream.async_stream_result()])
    assert flac.startswith(b"fLaC")
    pcm = await run("ffmpeg", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "24000", "pipe:1", stdin=flac)
    seconds = len(pcm) / 2 / 24000
    assert seconds == pytest.approx(3, abs=0.2)


async def test_options_change_voice_and_reload(hass: HomeAssistant, entry, azure) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    defaults = {str(key): key.default() for key in result["data_schema"].schema}
    assert defaults == NEURO_FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**NEURO_FORM, "short_name": "en-GB-RyanNeural", "pitch": "default"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["voice"] == "RyanNeural"
    assert entry.options["gender"] == "Male"
    assert entry.options["language"] == "en-gb"
    assert entry.state is ConfigEntryState.LOADED
    assert tts.get_engine_instance(hass, "tts.azure_ashleyneural").default_language == "en-gb"


async def test_reconfigure_replaces_key_and_keeps_voice(hass: HomeAssistant, entry, azure) -> None:
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "new", CONF_REGION: REGION}
    )
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_API_KEY] == "new"
    assert entry.options == NEURO_OPTIONS
    assert entry.state is ConfigEntryState.LOADED


async def test_reconfigure_refuses_region_without_the_voice(hass, entry, aioclient_mock) -> None:
    aioclient_mock.get("https://eastus.tts.speech.microsoft.com/cognitiveservices/voices/list", json=VOICES[1:])
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "new", CONF_REGION: "eastus"}
    )
    assert result["errors"] == {CONF_REGION: "voice_not_in_region"}
    assert entry.data[CONF_API_KEY] == "secret"
