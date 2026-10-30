"""Behavior tests for the Azure client, using a local fake of the Azure API."""

import asyncio
import sys
from pathlib import Path
from unittest import mock

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from pycsspeechtts import pycsspeechtts
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "custom_components/azure_streaming_tts"))

import speech  # noqa: E402
from speech import (  # noqa: E402
    AzureAuthError,
    AzureSpeechClient,
    AzureSpeechError,
    SentenceBuffer,
    VoiceSettings,
    async_list_voices,
)

SAMPLE = VoiceSettings("en-us", "Female", "AshleyNeural", "0%", "100%", "+25%", "")


def core_integration_body(text: str) -> bytes:
    """Return what core's microsoft integration sends for the same settings."""
    with mock.patch.object(pycsspeechtts, "requests") as requests:
        requests.post.return_value = mock.Mock(text="token", status_code=200)
        requests.codes.ok = 200
        pycsspeechtts.TTSTranslator("key", "germanywestcentral").speak(
            language="en-us",
            gender="Female",
            voiceType="AshleyNeural",
            output="audio-24khz-96kbitrate-mono-mp3",
            rate="0%",
            volume="100%",
            pitch="+25%",
            contour="",
            text=text,
        )
    return requests.post.call_args.args[1]


def test_ssml_is_byte_identical_to_core_integration():
    text = "Six lights are on. Very ambient."
    assert SAMPLE.ssml(text) == core_integration_body(text)


def test_ssml_carries_the_canonical_voice():
    body = SAMPLE.ssml("Hi").decode()
    assert "(en-US, AshleyNeural)" in body
    assert 'pitch="+25%"' in body


def test_ssml_escapes_text_the_core_library_would_crash_on():
    assert b"Q&amp;A &lt;now&gt;" in SAMPLE.ssml("Q&A <now>")


def split(*deltas: str) -> list[str]:
    buffer = SentenceBuffer()
    pieces = [piece for delta in deltas for piece in buffer.feed(delta)]
    if (rest := buffer.flush()) is not None:
        pieces.append(rest)
    return pieces


def test_sentences_survive_any_chunking():
    text = "Here is the first full sentence of this. And here is the second one, also long. Short end."
    expected = split(text)
    assert len(expected) == 2
    assert split(*text) == expected


def test_no_text_is_lost():
    text = "Oh. Good. Pain. Think again, because this sentence is long enough now. Mr. Frog is here!"
    assert " ".join(split(text)) == text


def test_short_fragments_merge_forward():
    assert split("Oh. Good. This next sentence is clearly long enough to stand. Done.")[0].startswith("Oh. Good.")


def test_abbreviation_does_not_end_a_sentence():
    pieces = split("I would like to introduce you to my friend Mr. Frog, who lives in a bucket. He reads minds.")
    assert any("Mr. Frog" in piece for piece in pieces)


def test_decimal_does_not_end_a_sentence():
    assert split("The temperature in the living room right now is 21.5 degrees.") == [
        "The temperature in the living room right now is 21.5 degrees."
    ]


def test_unending_sentence_is_cut_at_a_clause():
    pieces = split(*("word, " * 60))
    assert len(pieces) > 1
    assert " ".join(pieces).split() == ("word, " * 60).split()


def test_punctuation_only_is_never_sent():
    assert split("...") == []
    assert split("  \n ") == []


class FakeAzure:
    """Serve audio per request, recording what arrives and when audio starts."""

    def __init__(self, status: int = 200, fail_on_request: int | None = None) -> None:
        self.status = status
        self.fail_on_request = fail_on_request
        self.bodies: list[bytes] = []
        self.headers = []

    async def speak(self, request: web.Request) -> web.StreamResponse:
        self.bodies.append(await request.read())
        self.headers.append(request.headers)
        number = len(self.bodies)
        if self.status != 200 or number == self.fail_on_request:
            return web.Response(status=self.status if self.status != 200 else 500, text="nope")
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(f"a{number}".encode())
        await asyncio.sleep(0.01)
        await response.write(f"b{number}".encode())
        return response

    async def voices(self, request: web.Request) -> web.Response:
        if self.status != 200:
            return web.Response(status=self.status)
        return web.json_response(
            [{"ShortName": "en-US-AshleyNeural", "DisplayName": "Ashley", "Locale": "en-US", "Gender": "Female", "Name": SAMPLE.voice_name}]
        )


@pytest.fixture
async def run_azure(monkeypatch):
    servers, sessions = [], []

    async def start(fake: FakeAzure) -> AzureSpeechClient:
        app = web.Application()
        app.router.add_post("/cognitiveservices/v1", fake.speak)
        app.router.add_get("/cognitiveservices/voices/list", fake.voices)
        server = TestServer(app)
        await server.start_server()
        session = ClientSession()
        servers.append(server)
        sessions.append(session)
        monkeypatch.setattr(speech, "_service_url", lambda _: str(server.make_url("")).rstrip("/"))
        return AzureSpeechClient(session, "secret", "region", SAMPLE)

    yield start
    for session in sessions:
        await session.close()
    for server in servers:
        await server.close()


async def text_stream(*deltas: str):
    for delta in deltas:
        yield delta


async def test_audio_for_each_sentence_arrives_in_order(run_azure):
    fake = FakeAzure()
    client = await run_azure(fake)
    audio = b""
    async for chunk in client.async_stream_text(
        text_stream("This is the first sentence, and it is long. ", "This is the second sentence, and it is long too.")
    ):
        audio += chunk
    assert audio == b"a1b1a2b2"
    assert len(fake.bodies) == 2
    assert all(b'pitch="+25%"' in body for body in fake.bodies)
    assert fake.headers[0]["Ocp-Apim-Subscription-Key"] == "secret"


async def test_first_audio_is_sent_before_the_text_finishes(run_azure):
    fake = FakeAzure()
    client = await run_azure(fake)
    release = asyncio.Event()

    async def slow_text():
        yield "This first sentence is complete and long enough to go. "
        await release.wait()
        yield "This one only exists after audio has started."

    stream = client.async_stream_text(slow_text())
    first = await asyncio.wait_for(anext(stream), 2)
    assert first == b"a1"
    release.set()
    async for _ in stream:
        pass
    assert len(fake.bodies) == 2


async def test_azure_failure_raises_instead_of_going_silent(run_azure):
    client = await run_azure(FakeAzure(status=500))
    with pytest.raises(AzureSpeechError):
        async for _ in client.async_stream_text(text_stream("Some text to speak.")):
            pass


async def test_failure_midway_keeps_earlier_audio_then_raises(run_azure):
    client = await run_azure(FakeAzure(fail_on_request=2))
    audio = b""
    with pytest.raises(AzureSpeechError):
        async for chunk in client.async_stream_text(
            text_stream("This is the first sentence, and it is long. This is the second sentence, and it is long too.")
        ):
            audio += chunk
    assert audio == b"a1b1"


@pytest.mark.parametrize(("status", "error"), [(401, AzureAuthError), (403, AzureAuthError), (500, AzureSpeechError)])
async def test_voice_list_classifies_errors(run_azure, status, error):
    client = await run_azure(FakeAzure(status=status))
    with pytest.raises(error):
        await async_list_voices(client._session, "secret", "region")


async def test_voice_list_rebuilds_the_exact_voice_name(run_azure):
    client = await run_azure(FakeAzure())
    [voice] = await async_list_voices(client._session, "secret", "region")
    assert voice.voice == "AshleyNeural"
    assert voice.gender == "Female"
    rebuilt = VoiceSettings(voice.locale.lower(), voice.gender, voice.voice, "0%", "100%", "+25%", "")
    assert rebuilt.ssml("Hi") == SAMPLE.ssml("Hi")
