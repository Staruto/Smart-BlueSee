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
```

Start the server:

```powershell
python ws_server.py
```

Server prints a LAN URL such as `ws://192.168.x.x:8765` for ESP32 configuration.

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