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