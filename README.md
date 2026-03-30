# Smart-BlueSee

## ESP32 LAN WebSocket Server (v1)

This repository now includes a standalone LAN voice server for ESP32 clients:

- Entry point: `ws_server.py`
- Transport: WebSocket
- Input audio: PCM16, 16kHz, mono, chunked binary frames
- Output audio: PCM16, mono (sample rate returned in `done` message)
- Scope: single active device (stability-first)

### Run

Install dependency if needed:

```powershell
pip install websockets
pip install aiohttp
```

Start the server:

```powershell
python ws_server.py
```

Server prints a LAN URL such as `ws://192.168.x.x:8765` for ESP32 configuration.
It also prints an admin dashboard URL such as `http://192.168.x.x:8766/admin`.

Run local protocol tester in another terminal:

```powershell
python tests/test_ws_loopback.py --url ws://127.0.0.1:8765
```

### Protocol Summary

Client to server:

- Binary frame: append PCM16 audio chunk to current utterance buffer.
- JSON text frame:
	- `{"type":"start_utterance"}`
	- `{"type":"end_utterance"}`
	- `{"type":"ping"}`
	- `{"type":"reset"}`

Server to client:

- JSON text frames: `hello`, `state`, `text`, `done`, `error`, `ignored`, `pong`, `ok`
- Binary frame: synthesized reply audio bytes (PCM16)

### Config

WebSocket options are in `config.py`:

- `WS_HOST`, `WS_PORT`
- `WS_MAX_MESSAGE_BYTES`, `WS_IDLE_TIMEOUT_SEC`
- `WS_MAX_UTTERANCE_SEC`
- `WS_INPUT_SAMPLE_RATE`, `WS_INPUT_CHANNELS`, `WS_INPUT_SAMPLE_WIDTH_BYTES`
- `WS_LOG_VERBOSE`

Admin dashboard options in `config.py`:

- `ADMIN_HTTP_HOST`, `ADMIN_HTTP_PORT`
- `ADMIN_POLL_INTERVAL_MS`
- `ADMIN_ENABLE_VERBOSE_EVENTS`
- `MODULE_ASR_ENABLED`, `MODULE_LLM_ENABLED`, `MODULE_TTS_ENABLED`

Admin endpoints:

- `GET /api/admin/status`
- `GET /api/admin/events`
- `GET /api/admin/connections`
- `POST /api/admin/modules`
- `POST /api/admin/send-text`

Dashboard behavior (simplified default view):

- Shows key live summary fields only (active device, duration, latency, traffic, last error).
- Detailed raw JSON is available in a collapsible "Detailed JSON" section.
- Event panel defaults to `error + warning` and supports severity filtering.

Module switch behavior:

- ASR disabled: server returns `ASR_DISABLED` and skips utterance processing.
- LLM disabled: server returns a controlled maintenance text response.
- TTS disabled: server returns text response without audio bytes.

Admin text fallback behavior:

- `POST /api/admin/send-text` synthesizes provided text and pushes audio to the active ESP32 client.
- The pushed text is recorded into active session history as an admin broadcast.
- Common error codes: `NO_ACTIVE_CLIENT`, `EMPTY_TEXT`, `TTS_DISABLED`, `CLIENT_BUSY`.

Common integration issues:

- `ADMIN_TTS_ERROR: Model is multi-speaker but no speaker is provided`:
	The server now uses configured `TTS_SPEAKER` when synthesizing admin fallback text.
- `UTTERANCE_TOO_LONG`:
	This means buffered input audio exceeded `WS_MAX_UTTERANCE_SEC` limit. The error message includes current/max bytes to help MCU-side tuning.

Manual fallback test script:

```powershell
python tests/test_admin_fallback.py --ws-url ws://127.0.0.1:8765 --admin-base http://127.0.0.1:8766
```

### Web Search (Serper)

This project supports optional Web Search with automatic routing.

1. Set API key (PowerShell):

```powershell
$env:SERPER_API_KEY="your_serper_api_key"
```

2. Enable in `config.py`:

- `ENABLE_WEB_SEARCH = True`
- `WEB_SEARCH_PROVIDER = "serper"`

3. Optional tuning in `config.py`:

- `WEB_SEARCH_MAX_RESULTS`
- `WEB_SEARCH_TIMEOUT_SEC`
- `WEB_SEARCH_MIN_CONFIDENCE`
- `WEB_SEARCH_SOURCES_MAX`

Behavior:

- Campus questions still prefer local KB context.
- Deterministic questions (for example, current date/time) are answered locally first.
- Time-sensitive / low-KB-coverage questions can auto-trigger Web search.
- Social sources are down-ranked; higher-quality sources are preferred.
- Replies remain natural, with a short sources list when web evidence passes quality gates.
