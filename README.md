# Azure Streaming TTS

Home Assistant text-to-speech through Azure Speech, with audio streamed while the conversation agent is still writing its reply.

Core's `microsoft` integration waits for the whole reply and then generates one file. On a long reply, speech starts late. This integration sends each finished sentence to Azure and passes the audio on as it arrives.

## What it keeps from the `microsoft` integration

The request to Azure is byte-identical for the same text and settings, and `tests/test_speech.py` proves it against `pycsspeechtts`. The same voice, pitch, rate, volume, and output format (`audio-24khz-96kbitrate-mono-mp3`) are sent.

Sentences are generated separately, so the flow between two sentences can differ from one long request.

## What it adds

- Setup from the UI, with no `configuration.yaml` entry.
- A voice dropdown filled from your Azure Speech resource.
- Voice settings you can change from **Configure**, without a restart.
- **Reconfigure** to replace the key or region.
- Text is escaped, so a reply containing `&` or `<` no longer breaks the request.

## Install

Copy `custom_components/azure_streaming_tts` to your Home Assistant `config/custom_components` folder, or add this repository to HACS as a custom integration repository. Restart Home Assistant.

## Set up

1. Go to **Settings > Devices & services > Add integration > Azure Streaming TTS**.
2. Enter your Azure Speech key and region.
3. Choose the voice and its settings. To keep a voice from a `microsoft` YAML entry, copy its `pitch`, `rate`, `volume`, and `contour` values. If `rate` or `contour` is absent, use `0` and leave contour empty.
4. In your Assist pipeline, set text-to-speech to the new engine.

The YAML entry can stay in place as a fallback until you are happy with the result.

## Develop

The tests need Python 3.14.

```sh
pip install -r requirements-test.txt
pytest tests -p no:homeassistant
pytest tests_ha
```

`tests` covers the Azure client against a local fake server. `tests_ha` runs the UI flows and the streaming path inside a real Home Assistant, with Azure faked at the HTTP layer. Neither suite calls Azure.
