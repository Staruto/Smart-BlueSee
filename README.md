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

## MCU 联调当天你要做什么

按下面顺序做即可：

### 1) 启动你的服务端

```powershell
python ws_server.py
```

记下控制台输出的 LAN URL，例如 `ws://192.168.137.1:8765`。

### 2) 给 MCU 同学这 4 个参数

- IP: 你的 LAN IP（例如 192.168.137.1）
- Port: 8765（或你改过的 `WS_PORT`）
- URL: `ws://<你的IP>:<端口>`
- 音频格式: PCM16 / 16000 Hz / mono / 20ms 一帧（640 字节）

### 3) 确认两台设备在同一网络

- 你的电脑开热点，MCU 连上该热点。
- Windows 下确认本机 IPv4 地址与热点网段一致。

### 4) 允许防火墙入站（端口 8765）

首次运行若被 Windows 提示网络访问，选择允许。

若没有弹窗，可在管理员 PowerShell 手动放行：

```powershell
netsh advfirewall firewall add rule name="Bluesee WS 8765" dir=in action=allow protocol=TCP localport=8765
```

### 5) 按这个最小时序联调

1. MCU 建立 WebSocket 连接。
2. MCU 发送 `{"type":"start_utterance"}`。
3. MCU 按 20ms 连续发送二进制 PCM 帧。
4. MCU 发送 `{"type":"end_utterance"}`。
5. MCU 接收：
	- `state`（处理中）
	- `text`（ASR 和回复文本）
	- 二进制音频帧（TTS）
	- `done`（包含 `utterance_id` 和 `asr_ms/llm_ms/tts_ms/total_ms`）

### 6) 出问题时先看这几项

- 连不上：检查 URL、端口、热点是否同网段、防火墙是否放行。
- 收到 `BUSY`：当前服务器是单设备模式，先断开其他客户端。
- 收到 `EMPTY_AUDIO`：确认 MCU 的二进制帧确实发出且在 `end_utterance` 前送达。
- 收到 `UTTERANCE_TOO_LONG`：缩短一次说话时长，或调大 `WS_MAX_UTTERANCE_SEC`。
- 收到 `IDLE_TIMEOUT`：发送节奏中断过久，检查 MCU 发送循环。

### 7) 特别注意：两台都连到“同学PC热点”时

- 你必须使用你电脑在该热点下拿到的 IPv4 地址，不是你自己热点时的旧地址。
- 启动 `ws_server.py` 后，优先使用启动日志中的 `LAN IPv4s` 列表地址。
- 先在同学 PC 上做端口探测，再让 ESP32 连：

```powershell
Test-NetConnection <你的IP> -Port 8765
```

如果 `TcpTestSucceeded` 是 `False`，优先排查：

- 服务器是否正在运行。
- 你电脑防火墙是否放行 8765/TCP 入站。
- 热点是否启用了 AP Isolation / Client Isolation（若开启，终端之间互相不可见）。