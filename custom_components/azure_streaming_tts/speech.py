"""Azure Speech client and sentence splitting, free of Home Assistant imports."""

from collections.abc import AsyncGenerator, AsyncIterable
from dataclasses import dataclass
import re
from xml.etree import ElementTree

from aiohttp import ClientError, ClientSession, ClientTimeout

OUTPUT_FORMAT = "audio-24khz-96kbitrate-mono-mp3"
USER_AGENT = "ha-azure-streaming-tts"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
XML_GENDER = "{http://www.w3.org/XML/1998/namespace}gender"
TIMEOUT = ClientTimeout(total=None, sock_connect=10, sock_read=15)

# Very short fragments are merged forward so "Oh." keeps its intonation.
MIN_SENTENCE_CHARS = 40
# A sentence this long is cut at a clause so audio does not stall behind it.
MAX_SENTENCE_CHARS = 200

_SENTENCE_END = re.compile(r"[.!?…]+[\"'”’)\]]*\s+|\n+")
_CLAUSE_END = re.compile(r"[,;:—]\s+")
_ABBREVIATION = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|St|vs|etc|e\.g|i\.e)\.\s+$", re.IGNORECASE)


class AzureSpeechError(Exception):
    """Azure rejected a request or could not be reached."""


class AzureAuthError(AzureSpeechError):
    """Azure rejected the key for this region."""


@dataclass(frozen=True, slots=True)
class VoiceSettings:
    """Everything that decides how the voice sounds."""

    language: str
    gender: str
    voice: str
    rate: str
    volume: str
    pitch: str
    contour: str

    @property
    def voice_name(self) -> str:
        """Return the full service name of the voice."""
        first, *rest = self.language.split("-")
        locale = "-".join([first, *(part.upper() for part in rest)])
        return f"Microsoft Server Speech Text to Speech Voice ({locale}, {self.voice})"

    def ssml(self, text: str) -> bytes:
        """Render the request body, matching core's microsoft integration."""
        speak = ElementTree.Element("speak", version="1.0")
        speak.set(XML_LANG, self.language)
        speak.set("xmlns", "http://www.w3.org/2001/10/synthesis")
        speak.set("xmlns:mstts", "http://www.w3.org/2001/mstts")
        voice = ElementTree.SubElement(speak, "voice")
        voice.set(XML_LANG, self.language)
        voice.set(XML_GENDER, self.gender)
        voice.set("name", self.voice_name)
        prosody = ElementTree.SubElement(voice, "prosody")
        prosody.text = text
        prosody.set("rate", self.rate)
        prosody.set("volume", self.volume)
        prosody.set("pitch", self.pitch)
        prosody.set("contour", self.contour)
        return ElementTree.tostring(speak)


class SentenceBuffer:
    """Cut streamed text into pieces that are safe to synthesize separately."""

    def __init__(self) -> None:
        """Start with no pending text."""
        self._pending = ""

    def feed(self, text: str) -> list[str]:
        """Add text and return every piece that is now complete."""
        self._pending += text
        pieces = []
        while (cut := self._find_cut()) is not None:
            piece, self._pending = self._pending[:cut], self._pending[cut:]
            if _is_speakable(piece):
                pieces.append(piece.strip())
        return pieces

    def flush(self) -> str | None:
        """Return whatever is left once the text has ended."""
        piece, self._pending = self._pending.strip(), ""
        return piece if _is_speakable(piece) else None

    def _find_cut(self) -> int | None:
        for match in _SENTENCE_END.finditer(self._pending):
            if match.end() < MIN_SENTENCE_CHARS:
                continue
            if _ABBREVIATION.search(self._pending[: match.end()]):
                continue
            return match.end()
        if len(self._pending) >= MAX_SENTENCE_CHARS:
            clauses = list(_CLAUSE_END.finditer(self._pending))
            if clauses and clauses[-1].end() >= MIN_SENTENCE_CHARS:
                return clauses[-1].end()
        return None


def _is_speakable(text: str) -> bool:
    return any(char.isalnum() for char in text)


@dataclass(frozen=True, slots=True)
class AzureVoice:
    """One voice Azure offers in a region."""

    short_name: str
    display_name: str
    locale: str
    gender: str

    @property
    def voice(self) -> str:
        """Return the name without its locale, as in AshleyNeural."""
        return self.short_name.removeprefix(f"{self.locale}-")


def _service_url(region: str) -> str:
    return f"https://{region}.tts.speech.microsoft.com"


async def async_list_voices(
    session: ClientSession, api_key: str, region: str
) -> list[AzureVoice]:
    """Check the key and return the voices this region offers."""
    try:
        async with session.get(
            f"{_service_url(region)}/cognitiveservices/voices/list",
            headers={"Ocp-Apim-Subscription-Key": api_key},
            timeout=TIMEOUT,
        ) as response:
            await _raise_for_status(response)
            voices = await response.json()
    except (ClientError, TimeoutError) as err:
        raise AzureSpeechError(str(err)) from err
    return [
        AzureVoice(voice["ShortName"], voice["DisplayName"], voice["Locale"], voice["Gender"])
        for voice in voices
    ]


class AzureSpeechClient:
    """Talk to one Azure Speech resource with one fixed voice."""

    def __init__(
        self, session: ClientSession, api_key: str, region: str, settings: VoiceSettings
    ) -> None:
        """Store the connection details."""
        self._session = session
        self._api_key = api_key
        self._region = region
        self._settings = settings

    async def async_stream_sentence(self, text: str) -> AsyncGenerator[bytes]:
        """Yield audio for one piece of text as Azure produces it."""
        try:
            async with self._session.post(
                f"{_service_url(self._region)}/cognitiveservices/v1",
                data=self._settings.ssml(text),
                headers={
                    "Content-Type": "application/ssml+xml",
                    "Ocp-Apim-Subscription-Key": self._api_key,
                    "User-Agent": USER_AGENT,
                    "X-Microsoft-OutputFormat": OUTPUT_FORMAT,
                },
                timeout=TIMEOUT,
            ) as response:
                await _raise_for_status(response)
                async for chunk in response.content.iter_any():
                    yield chunk
        except (ClientError, TimeoutError) as err:
            raise AzureSpeechError(str(err)) from err

    async def async_stream_text(
        self, message_gen: AsyncIterable[str]
    ) -> AsyncGenerator[bytes]:
        """Yield audio for streamed text, one sentence at a time, in order."""
        sentences = SentenceBuffer()
        async for text in message_gen:
            for sentence in sentences.feed(text):
                async for chunk in self.async_stream_sentence(sentence):
                    yield chunk
        if (remainder := sentences.flush()) is not None:
            async for chunk in self.async_stream_sentence(remainder):
                yield chunk


async def _raise_for_status(response) -> None:
    if response.status in (401, 403):
        raise AzureAuthError(f"Azure rejected the key (HTTP {response.status})")
    if response.status != 200:
        detail = (await response.text())[:200]
        raise AzureSpeechError(f"Azure returned HTTP {response.status}: {detail}")
