"""
WebSocket server for ESP32 LAN voice interaction.

Protocol v1 (single device stable-first):
- Binary frame: raw PCM16 mono chunks at WS_INPUT_SAMPLE_RATE.
- Text frame (JSON): control messages.
  - {"type": "start_utterance"}
  - {"type": "end_utterance"}
  - {"type": "ping"}
  - {"type": "reset"}

Server responses:
- JSON text frames for state/errors.
- Binary frame for synthesized reply audio (PCM16 mono).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import socket
import tempfile
import time
import wave
from dataclasses import dataclass, field

import numpy as np
import websockets  # type: ignore[reportMissingImports]
from websockets.exceptions import ConnectionClosed  # type: ignore[reportMissingImports]
from websockets.server import WebSocketServerProtocol  # type: ignore[reportMissingImports]

from config import (
    ASSISTANT_NAME,
    MAX_CONTEXT_TURNS,
    MAX_GENERATION_TOKENS,
    REPEAT_PENALTY,
    TEMPERATURE,
    TOP_P,
    WHISPER_LANGUAGE,
    WS_HOST,
    WS_IDLE_TIMEOUT_SEC,
    WS_INPUT_CHANNELS,
    WS_INPUT_SAMPLE_RATE,
    WS_INPUT_SAMPLE_WIDTH_BYTES,
    WS_LOG_VERBOSE,
    WS_MAX_MESSAGE_BYTES,
    WS_MAX_UTTERANCE_SEC,
    WS_PORT,
)
from client_v4 import (
    check_emergency,
    get_system_prompt,
    kb,
    llm,
    should_ignore,
    tts_engine,
    asr_model,
)

_SENTENCE_END_PATTERN = re.compile(r"(?<=[.!?。！？])\s*")


@dataclass
class SessionState:
    history: list[tuple[str, str]] = field(default_factory=list)
    summary: str = ""


def _log(msg: str):
    if WS_LOG_VERBOSE:
        print(f"[WS] {msg}")


def _safe_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False)


async def _send_json(ws: WebSocketServerProtocol, payload: dict):
    await ws.send(_safe_json(payload))


async def _safe_send_json(ws: WebSocketServerProtocol, payload: dict):
    with contextlib.suppress(ConnectionClosed):
        await _send_json(ws, payload)


def _local_lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _candidate_lan_ips() -> list[str]:
    ips: set[str] = set()

    preferred = _local_lan_ip()
    if preferred != "127.0.0.1":
        ips.add(preferred)

    with contextlib.suppress(OSError):
        host = socket.gethostname()
        for ip in socket.gethostbyname_ex(host)[2]:
            if ip.startswith("127."):
                continue
            ips.add(ip)

    # Show private-LAN addresses first, then others.
    def _priority(ip: str) -> int:
        if ip.startswith("192.168."):
            return 0
        if ip.startswith("10."):
            return 1
        if ip.startswith("172."):
            try:
                second = int(ip.split(".")[1])
            except (IndexError, ValueError):
                return 3
            if 16 <= second <= 31:
                return 2
        return 3

    return sorted(ips, key=lambda x: (_priority(x), x))


def _max_utterance_bytes() -> int:
    return (
        WS_MAX_UTTERANCE_SEC
        * WS_INPUT_SAMPLE_RATE
        * WS_INPUT_CHANNELS
        * WS_INPUT_SAMPLE_WIDTH_BYTES
    )


def _append_summary_if_needed(session: SessionState):
    threshold = MAX_CONTEXT_TURNS * 2
    if len(session.history) <= threshold:
        return

    cutoff = len(session.history) // 2
    old_turns = session.history[:cutoff]
    lines: list[str] = []
    for role, content in old_turns:
        tag = "User" if role == "user" else "Assistant"
        short = content[:120].replace("\n", " ")
        if len(content) > 120:
            short += "..."
        lines.append(f"- {tag}: {short}")

    added = "\n".join(lines)
    if session.summary:
        session.summary += "\n" + added
    else:
        session.summary = added
    del session.history[:cutoff]


def _build_prompt_for_session(session: SessionState, user_input: str) -> str:
    context, kb_stats = kb.retrieve_debug(user_input)
    _log(
        "KB sections=%s chars=%s tokens_est=%s"
        % (
            kb_stats.get("sections_used", "?"),
            kb_stats.get("context_chars", "?"),
            kb_stats.get("context_tokens_est", "?"),
        )
    )

    system_prompt = get_system_prompt(context)
    history_block = ""

    if session.summary:
        history_block += (
            "<|start_header_id|>system<|end_header_id|>\n"
            "Summary of earlier conversation:\n"
            f"{session.summary}\n"
            "<|eot_id|>\n"
        )

    for role, content in session.history[-MAX_CONTEXT_TURNS * 2 :]:
        history_block += (
            f"<|start_header_id|>{role}<|end_header_id|>\n"
            f"{content}\n"
            "<|eot_id|>\n"
        )

    return (
        system_prompt
        + history_block
        + "<|start_header_id|>user<|end_header_id|>\n"
        + user_input
        + "\n<|eot_id|>\n"
        + "<|start_header_id|>assistant<|end_header_id|>\n"
    )


def _transcribe_pcm16_bytes(pcm_bytes: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_wav:
        wav_path = temp_wav.name

    try:
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(WS_INPUT_CHANNELS)
            wf.setsampwidth(WS_INPUT_SAMPLE_WIDTH_BYTES)
            wf.setframerate(WS_INPUT_SAMPLE_RATE)
            wf.writeframes(pcm_bytes)

        result = asr_model.transcribe(
            wav_path,
            fp16=False,
            language=WHISPER_LANGUAGE,
        )
        return str(result.get("text", "")).strip()
    finally:
        with contextlib.suppress(OSError):
            import os

            os.remove(wav_path)


def _generate_reply_text(session: SessionState, user_text: str) -> str:
    emergency = check_emergency(user_text)
    if emergency:
        return emergency

    _append_summary_if_needed(session)
    prompt = _build_prompt_for_session(session, user_text)

    stream = llm(
        prompt,
        max_tokens=MAX_GENERATION_TOKENS,
        stop=["<|eot_id|>"],
        temperature=TEMPERATURE,
        top_p=TOP_P,
        repeat_penalty=REPEAT_PENALTY,
        stream=True,
    )

    full = []
    for chunk in stream:
        token = chunk["choices"][0]["text"]
        full.append(token)

    response = "".join(full).strip()
    return response


def _synthesize_to_pcm16(reply_text: str) -> tuple[bytes, int]:
    with contextlib.redirect_stdout(io.StringIO()):
        wav = tts_engine.tts(text=reply_text)

    wav_np = np.array(wav, dtype=np.float32)
    audio_int16 = (wav_np * 32767).clip(-32768, 32767).astype(np.int16)
    sample_rate = int(getattr(tts_engine.synthesizer, "output_sample_rate", 22050))
    return audio_int16.tobytes(), sample_rate


_active_client_guard = asyncio.Lock()
_active_client_id: str | None = None


async def _claim_active_client(client_id: str) -> bool:
    global _active_client_id
    async with _active_client_guard:
        if _active_client_id is not None:
            return False
        _active_client_id = client_id
        return True


async def _release_active_client(client_id: str):
    global _active_client_id
    async with _active_client_guard:
        if _active_client_id == client_id:
            _active_client_id = None


async def handle_client(ws: WebSocketServerProtocol):
    client_id = f"{ws.remote_address}"
    claimed = await _claim_active_client(client_id)
    if not claimed:
        await _send_json(
            ws,
            {
                "type": "error",
                "code": "BUSY",
                "message": "Server currently supports one active device.",
            },
        )
        await ws.close(code=1013, reason="Single-device mode busy")
        return

    _log(f"connected: {client_id}")
    await _send_json(
        ws,
        {
            "type": "hello",
            "protocol": "esp32-ws-v1",
            "input": {
                "encoding": "pcm16",
                "sample_rate": WS_INPUT_SAMPLE_RATE,
                "channels": WS_INPUT_CHANNELS,
            },
        },
    )

    session = SessionState()
    buffer = bytearray()
    max_bytes = _max_utterance_bytes()
    utterance_id = 0

    try:
        while True:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=WS_IDLE_TIMEOUT_SEC)
            except (asyncio.TimeoutError, TimeoutError):
                _log(f"idle timeout: {client_id} ({WS_IDLE_TIMEOUT_SEC}s without frames)")
                await _safe_send_json(
                    ws,
                    {
                        "type": "error",
                        "code": "IDLE_TIMEOUT",
                        "message": "No incoming frames within timeout window.",
                    },
                )
                with contextlib.suppress(ConnectionClosed):
                    await ws.close(code=1000, reason="Idle timeout")
                break

            if isinstance(message, bytes):
                if len(message) > WS_MAX_MESSAGE_BYTES:
                    await _send_json(
                        ws,
                        {
                            "type": "error",
                            "code": "FRAME_TOO_LARGE",
                            "message": "Binary frame exceeds WS_MAX_MESSAGE_BYTES.",
                        },
                    )
                    continue

                buffer.extend(message)
                if len(buffer) > max_bytes:
                    buffer.clear()
                    await _send_json(
                        ws,
                        {
                            "type": "error",
                            "code": "UTTERANCE_TOO_LONG",
                            "message": "Buffered audio exceeds WS_MAX_UTTERANCE_SEC.",
                        },
                    )
                    continue
                continue

            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                await _send_json(
                    ws,
                    {
                        "type": "error",
                        "code": "BAD_JSON",
                        "message": "Control frame must be valid JSON.",
                    },
                )
                continue

            msg_type = payload.get("type")

            if msg_type == "ping":
                await _send_json(ws, {"type": "pong"})
                continue

            if msg_type == "reset":
                session = SessionState()
                buffer.clear()
                await _send_json(ws, {"type": "ok", "message": "session reset"})
                continue

            if msg_type == "start_utterance":
                buffer.clear()
                await _send_json(ws, {"type": "ok", "message": "utterance started"})
                continue

            if msg_type != "end_utterance":
                await _send_json(
                    ws,
                    {
                        "type": "error",
                        "code": "UNKNOWN_TYPE",
                        "message": "Unsupported control frame type.",
                    },
                )
                continue

            if not buffer:
                await _send_json(
                    ws,
                    {
                        "type": "error",
                        "code": "EMPTY_AUDIO",
                        "message": "No buffered audio to process.",
                    },
                )
                continue

            pcm_bytes = bytes(buffer)
            buffer.clear()
            utterance_id += 1
            total_started = time.perf_counter()

            await _send_json(
                ws,
                {
                    "type": "state",
                    "state": "processing",
                    "utterance_id": utterance_id,
                    "input_bytes": len(pcm_bytes),
                },
            )

            try:
                asr_started = time.perf_counter()
                asr_text = _transcribe_pcm16_bytes(pcm_bytes)
                asr_ms = int((time.perf_counter() - asr_started) * 1000)
                if should_ignore(asr_text):
                    await _send_json(
                        ws,
                        {
                            "type": "ignored",
                            "utterance_id": utterance_id,
                            "reason": "noisy_or_short_asr",
                            "asr_ms": asr_ms,
                        },
                    )
                    continue

                llm_started = time.perf_counter()
                reply_text = _generate_reply_text(session, asr_text)
                llm_ms = int((time.perf_counter() - llm_started) * 1000)

                session.history.append(("user", asr_text))
                session.history.append(("assistant", reply_text))

                tts_started = time.perf_counter()
                audio_bytes, sample_rate = _synthesize_to_pcm16(reply_text)
                tts_ms = int((time.perf_counter() - tts_started) * 1000)
                total_ms = int((time.perf_counter() - total_started) * 1000)

                await _send_json(
                    ws,
                    {
                        "type": "text",
                        "utterance_id": utterance_id,
                        "asr_text": asr_text,
                        "reply_text": reply_text,
                    },
                )
                await ws.send(audio_bytes)
                await _send_json(
                    ws,
                    {
                        "type": "done",
                        "utterance_id": utterance_id,
                        "encoding": "pcm16",
                        "sample_rate": sample_rate,
                        "channels": 1,
                        "bytes": len(audio_bytes),
                        "asr_ms": asr_ms,
                        "llm_ms": llm_ms,
                        "tts_ms": tts_ms,
                        "total_ms": total_ms,
                        "assistant": ASSISTANT_NAME,
                    },
                )
            except Exception as exc:
                await _send_json(
                    ws,
                    {
                        "type": "error",
                        "utterance_id": utterance_id,
                        "code": "PROCESSING_ERROR",
                        "message": str(exc),
                    },
                )
    except ConnectionClosed:
        _log(f"disconnected: {client_id}")
    except asyncio.TimeoutError:
        # Defensive guard for event-loop timeout propagation from websocket internals.
        _log(f"connection timeout propagated: {client_id}")
    finally:
        await _release_active_client(client_id)


async def main():
    lan_ip = _local_lan_ip()
    candidate_ips = _candidate_lan_ips()
    print("=" * 60)
    print("ESP32 WebSocket voice server")
    print(f"Bind      : ws://{WS_HOST}:{WS_PORT}")
    print(f"LAN URL   : ws://{lan_ip}:{WS_PORT}")
    if candidate_ips:
        print("LAN IPs   : " + ", ".join(candidate_ips))
    print(f"Input PCM : 16-bit, {WS_INPUT_SAMPLE_RATE}Hz, mono")
    print("Mode      : single active device")
    print("=" * 60)

    async with websockets.serve(
        handle_client,
        WS_HOST,
        WS_PORT,
        max_size=WS_MAX_MESSAGE_BYTES,
        ping_interval=20,
        ping_timeout=20,
    ):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
