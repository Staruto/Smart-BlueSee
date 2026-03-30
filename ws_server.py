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
import os
import re
import socket
import tempfile
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from aiohttp import web
import websockets  # type: ignore[reportMissingImports]
from websockets.exceptions import ConnectionClosed  # type: ignore[reportMissingImports]
from websockets.server import WebSocketServerProtocol  # type: ignore[reportMissingImports]

from config import (
    ADMIN_ENABLE_VERBOSE_EVENTS,
    ADMIN_HTTP_HOST,
    ADMIN_HTTP_PORT,
    ADMIN_POLL_INTERVAL_MS,
    ASSISTANT_NAME,
    MAX_CONTEXT_TURNS,
    MAX_GENERATION_TOKENS,
    MODULE_ASR_ENABLED,
    MODULE_LLM_ENABLED,
    MODULE_TTS_ENABLED,
    REPEAT_PENALTY,
    TEMPERATURE,
    TTS_SPEAKER,
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
    WEB_SEARCH_SOURCES_MAX,
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
from local_tools import maybe_answer_local
from web_search import (
    build_sources_list,
    build_web_context,
    classify_query_intent,
    maybe_web_search,
    should_use_kb,
)

_SENTENCE_END_PATTERN = re.compile(r"(?<=[.!?。！？])\s*")
_ADMIN_DIR = Path(__file__).resolve().parent / "admin"


@dataclass
class ModuleState:
    asr_enabled: bool = MODULE_ASR_ENABLED
    llm_enabled: bool = MODULE_LLM_ENABLED
    tts_enabled: bool = MODULE_TTS_ENABLED


@dataclass
class RuntimeMetrics:
    utterances_total: int = 0
    admin_tts_push_total: int = 0
    input_audio_bytes_total: int = 0
    output_audio_bytes_total: int = 0
    asr_ms_avg: float = 0.0
    llm_ms_avg: float = 0.0
    tts_ms_avg: float = 0.0
    total_ms_avg: float = 0.0


@dataclass
class ConnectionSnapshot:
    client_id: str
    remote_ip: str
    remote_port: int
    connected_at: float
    disconnected_at: float | None = None
    status: str = "active"
    utterances: int = 0
    input_audio_bytes: int = 0
    output_audio_bytes: int = 0
    last_asr_text: str = ""
    last_reply_text: str = ""
    last_route_reason: str = ""

    def to_dict(self) -> dict:
        now_ts = time.time()
        end_ts = self.disconnected_at if self.disconnected_at else now_ts
        return {
            "client_id": self.client_id,
            "remote_ip": self.remote_ip,
            "remote_port": self.remote_port,
            "status": self.status,
            "connected_at": self.connected_at,
            "disconnected_at": self.disconnected_at,
            "duration_sec": int(max(0.0, end_ts - self.connected_at)),
            "utterances": self.utterances,
            "input_audio_bytes": self.input_audio_bytes,
            "output_audio_bytes": self.output_audio_bytes,
            "last_asr_text": self.last_asr_text,
            "last_reply_text": self.last_reply_text,
            "last_route_reason": self.last_route_reason,
        }


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


def _trim_text(text: str, limit: int = 240) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _rolling_avg(current_avg: float, count: int, new_value: int) -> float:
    if count <= 1:
        return float(new_value)
    return ((current_avg * (count - 1)) + new_value) / count


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


def _build_prompt_for_session(session: SessionState, user_input: str) -> tuple[str, list[str], str]:
    intent = classify_query_intent(user_input)
    use_kb = should_use_kb(user_input)
    if use_kb:
        context, kb_stats = kb.retrieve_debug(user_input)
    else:
        context, kb_stats = "", {
            "sections_used": 0,
            "context_chars": 0,
            "context_tokens_est": 0,
            "details": [],
        }

    web_results, route_reason = maybe_web_search(user_input, kb_stats)
    web_context = build_web_context(web_results)
    merged_context = context
    if web_context:
        merged_context += "\n\n[Web Evidence]\n" + web_context

    _log(
        "intent=%s KB sections=%s chars=%s tokens_est=%s use_kb=%s"
        % (
            intent,
            kb_stats.get("sections_used", "?"),
            kb_stats.get("context_chars", "?"),
            kb_stats.get("context_tokens_est", "?"),
            use_kb,
        )
    )
    if web_results:
        _log(f"Web results={len(web_results)} reason={route_reason}")
    else:
        _log(f"Web skipped reason={route_reason}")

    system_prompt = get_system_prompt(merged_context, has_web_context=bool(web_context))
    sources = build_sources_list(web_results, WEB_SEARCH_SOURCES_MAX)
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

    prompt = (
        system_prompt
        + history_block
        + "<|start_header_id|>user<|end_header_id|>\n"
        + user_input
        + "\n<|eot_id|>\n"
        + "<|start_header_id|>assistant<|end_header_id|>\n"
    )
    return prompt, sources, route_reason


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


def _generate_reply_text(session: SessionState, user_text: str) -> tuple[str, list[str], str]:
    emergency = check_emergency(user_text)
    if emergency:
        return emergency, [], "emergency"

    local_answer, local_reason = maybe_answer_local(user_text)
    if local_answer:
        return local_answer, [], local_reason

    _append_summary_if_needed(session)
    prompt, sources, route_reason = _build_prompt_for_session(session, user_text)

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
    return response, sources, route_reason


def _synthesize_to_pcm16(reply_text: str) -> tuple[bytes, int]:
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            wav = tts_engine.tts(text=reply_text, speaker=TTS_SPEAKER)
        except Exception:
            # Fallback for single-speaker models or model-specific speaker behavior.
            wav = tts_engine.tts(text=reply_text)

    wav_np = np.array(wav, dtype=np.float32)
    audio_int16 = (wav_np * 32767).clip(-32768, 32767).astype(np.int16)
    sample_rate = int(getattr(tts_engine.synthesizer, "output_sample_rate", 22050))
    return audio_int16.tobytes(), sample_rate


_active_client_guard = asyncio.Lock()
_active_client_id: str | None = None
_metrics_guard = asyncio.Lock()

_server_started_at = time.time()
_modules = ModuleState()
_metrics = RuntimeMetrics()
_active_connection: ConnectionSnapshot | None = None
_active_ws: WebSocketServerProtocol | None = None
_active_session: SessionState | None = None
_active_processing = False
_connection_history: list[dict] = []
_event_log: list[dict] = []
_admin_message_counter = 0


async def _record_event(event_type: str, **details):
    if not ADMIN_ENABLE_VERBOSE_EVENTS and event_type == "debug":
        return

    async with _metrics_guard:
        _event_log.append(
            {
                "ts": time.time(),
                "type": event_type,
                "details": details,
            }
        )
        if len(_event_log) > 300:
            del _event_log[: len(_event_log) - 300]


def _event_severity(event_type: str) -> str:
    if event_type in {
        "error",
        "idle_timeout",
    }:
        return "error"
    if event_type in {
        "module_block",
        "ignored",
    }:
        return "warning"
    if event_type == "debug":
        return "debug"
    return "info"


def _next_admin_message_id() -> str:
    global _admin_message_counter
    _admin_message_counter += 1
    return f"admin_{int(time.time() * 1000)}_{_admin_message_counter}"


def _format_mib(byte_count: int) -> float:
    return round(byte_count / (1024 * 1024), 2)


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
    global _active_connection, _active_session, _active_ws, _active_processing

    remote = ws.remote_address or ("unknown", 0)
    remote_ip = str(remote[0])
    remote_port = int(remote[1]) if len(remote) > 1 else 0
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

    connection_start = time.time()
    async with _metrics_guard:
        _active_connection = ConnectionSnapshot(
            client_id=client_id,
            remote_ip=remote_ip,
            remote_port=remote_port,
            connected_at=connection_start,
        )

    await _record_event("connect", client_id=client_id, remote_ip=remote_ip, remote_port=remote_port)
    _log(f"connected: {client_id}")
    await _send_json(
        ws,
        {
            "type": "hello",
            "protocol": "esp32-ws-v1",
            "client_id": client_id,
            "input": {
                "encoding": "pcm16",
                "sample_rate": WS_INPUT_SAMPLE_RATE,
                "channels": WS_INPUT_CHANNELS,
            },
        },
    )

    session = SessionState()
    async with _metrics_guard:
        _active_session = session
        _active_ws = ws

    buffer = bytearray()
    max_bytes = _max_utterance_bytes()
    utterance_id = 0

    try:
        while True:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=WS_IDLE_TIMEOUT_SEC)
            except (asyncio.TimeoutError, TimeoutError):
                _log(f"idle timeout: {client_id} ({WS_IDLE_TIMEOUT_SEC}s without frames)")
                await _record_event("idle_timeout", client_id=client_id)
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
                async with _metrics_guard:
                    if _active_connection and _active_connection.client_id == client_id:
                        _active_connection.input_audio_bytes += len(message)

                if len(message) > WS_MAX_MESSAGE_BYTES:
                    await _record_event("error", code="FRAME_TOO_LARGE", client_id=client_id)
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
                    current_bytes = len(buffer)
                    buffer.clear()
                    await _record_event(
                        "error",
                        code="UTTERANCE_TOO_LONG",
                        client_id=client_id,
                        current_bytes=current_bytes,
                        max_bytes=max_bytes,
                        max_utterance_sec=WS_MAX_UTTERANCE_SEC,
                    )
                    await _send_json(
                        ws,
                        {
                            "type": "error",
                            "code": "UTTERANCE_TOO_LONG",
                            "message": (
                                "Buffered audio exceeded utterance limit: "
                                f"{current_bytes} > {max_bytes} bytes "
                                f"(WS_MAX_UTTERANCE_SEC={WS_MAX_UTTERANCE_SEC})."
                            ),
                        },
                    )
                    continue
                continue

            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                await _record_event("error", code="BAD_JSON", client_id=client_id)
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
                await _record_event("reset", client_id=client_id)
                await _send_json(ws, {"type": "ok", "message": "session reset"})
                continue

            if msg_type == "start_utterance":
                buffer.clear()
                await _send_json(ws, {"type": "ok", "message": "utterance started"})
                continue

            if msg_type != "end_utterance":
                await _record_event("error", code="UNKNOWN_TYPE", client_id=client_id, msg_type=msg_type)
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
                await _record_event("error", code="EMPTY_AUDIO", client_id=client_id)
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

            async with _metrics_guard:
                _active_processing = True

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
                if not _modules.asr_enabled:
                    await _record_event("module_block", module="asr", client_id=client_id)
                    await _send_json(
                        ws,
                        {
                            "type": "error",
                            "utterance_id": utterance_id,
                            "code": "ASR_DISABLED",
                            "message": "ASR module is disabled by admin.",
                        },
                    )
                    continue

                asr_started = time.perf_counter()
                asr_text = _transcribe_pcm16_bytes(pcm_bytes)
                asr_ms = int((time.perf_counter() - asr_started) * 1000)
                if should_ignore(asr_text):
                    await _record_event("ignored", client_id=client_id, reason="noisy_or_short_asr")
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

                if _modules.llm_enabled:
                    llm_started = time.perf_counter()
                    reply_text, sources, route_reason = _generate_reply_text(session, asr_text)
                    llm_ms = int((time.perf_counter() - llm_started) * 1000)
                else:
                    reply_text = "LLM module is currently disabled by admin."
                    sources = []
                    route_reason = "llm_disabled"
                    llm_ms = 0

                session.history.append(("user", asr_text))
                session.history.append(("assistant", reply_text))

                audio_disabled = not _modules.tts_enabled
                if not audio_disabled:
                    tts_started = time.perf_counter()
                    audio_bytes, sample_rate = _synthesize_to_pcm16(reply_text)
                    tts_ms = int((time.perf_counter() - tts_started) * 1000)
                else:
                    audio_bytes = b""
                    sample_rate = 0
                    tts_ms = 0

                total_ms = int((time.perf_counter() - total_started) * 1000)

                async with _metrics_guard:
                    _metrics.utterances_total += 1
                    _metrics.input_audio_bytes_total += len(pcm_bytes)
                    _metrics.output_audio_bytes_total += len(audio_bytes)

                    count = _metrics.utterances_total
                    _metrics.asr_ms_avg = _rolling_avg(_metrics.asr_ms_avg, count, asr_ms)
                    _metrics.llm_ms_avg = _rolling_avg(_metrics.llm_ms_avg, count, llm_ms)
                    _metrics.tts_ms_avg = _rolling_avg(_metrics.tts_ms_avg, count, tts_ms)
                    _metrics.total_ms_avg = _rolling_avg(_metrics.total_ms_avg, count, total_ms)

                    if _active_connection and _active_connection.client_id == client_id:
                        _active_connection.utterances += 1
                        _active_connection.output_audio_bytes += len(audio_bytes)
                        _active_connection.last_asr_text = _trim_text(asr_text)
                        _active_connection.last_reply_text = _trim_text(reply_text)
                        _active_connection.last_route_reason = route_reason

                await _record_event(
                    "utterance_done",
                    client_id=client_id,
                    utterance_id=utterance_id,
                    asr_ms=asr_ms,
                    llm_ms=llm_ms,
                    tts_ms=tts_ms,
                    total_ms=total_ms,
                    route_reason=route_reason,
                    audio_disabled=audio_disabled,
                )

                await _send_json(
                    ws,
                    {
                        "type": "text",
                        "utterance_id": utterance_id,
                        "asr_text": asr_text,
                        "reply_text": reply_text,
                        "sources": sources,
                        "web_route": route_reason,
                        "connection_duration_sec": int(time.time() - connection_start),
                    },
                )
                if not audio_disabled:
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
                        "input_bytes": len(pcm_bytes),
                        "audio_disabled": audio_disabled,
                        "asr_ms": asr_ms,
                        "llm_ms": llm_ms,
                        "tts_ms": tts_ms,
                        "total_ms": total_ms,
                        "assistant": ASSISTANT_NAME,
                    },
                )
            except Exception as exc:
                await _record_event("error", code="PROCESSING_ERROR", client_id=client_id, message=str(exc))
                await _send_json(
                    ws,
                    {
                        "type": "error",
                        "utterance_id": utterance_id,
                        "code": "PROCESSING_ERROR",
                        "message": str(exc),
                    },
                )
            finally:
                async with _metrics_guard:
                    _active_processing = False
    except ConnectionClosed:
        await _record_event("disconnect", client_id=client_id, reason="connection_closed")
        _log(f"disconnected: {client_id}")
    except asyncio.TimeoutError:
        # Defensive guard for event-loop timeout propagation from websocket internals.
        await _record_event("disconnect", client_id=client_id, reason="connection_timeout")
        _log(f"connection timeout propagated: {client_id}")
    finally:
        async with _metrics_guard:
            if _active_connection and _active_connection.client_id == client_id:
                _active_connection.status = "disconnected"
                _active_connection.disconnected_at = time.time()
                _connection_history.append(_active_connection.to_dict())
                if len(_connection_history) > 100:
                    del _connection_history[: len(_connection_history) - 100]
                _active_connection = None
            _active_session = None
            _active_ws = None
            _active_processing = False

        await _release_active_client(client_id)


async def _admin_index(_request: web.Request) -> web.Response:
    index_path = _ADMIN_DIR / "index.html"
    if not index_path.exists():
        return web.Response(status=404, text="admin/index.html not found")
    return web.FileResponse(index_path)


async def _admin_status(_request: web.Request) -> web.Response:
    async with _metrics_guard:
        active_client_id = _active_client_id
        active = _active_connection.to_dict() if _active_connection else None
        active_summary = None
        if active:
            active_summary = {
                "remote": f"{active['remote_ip']}:{active['remote_port']}",
                "status": active["status"],
                "duration_sec": active["duration_sec"],
                "utterances": active["utterances"],
                "input_mib": _format_mib(active["input_audio_bytes"]),
                "output_mib": _format_mib(active["output_audio_bytes"]),
                "last_route_reason": active["last_route_reason"],
            }
        elif active_client_id:
            active_summary = {
                "remote": str(active_client_id),
                "status": "connected",
                "duration_sec": 0,
                "utterances": 0,
                "input_mib": 0.0,
                "output_mib": 0.0,
                "last_route_reason": "",
            }

        latest_error = None
        for evt in reversed(_event_log):
            if _event_severity(evt["type"]) == "error":
                latest_error = evt
                break

        payload = {
            "uptime_sec": int(time.time() - _server_started_at),
            "poll_interval_ms": ADMIN_POLL_INTERVAL_MS,
            "active_client_count": 1 if (_active_connection or active_client_id) else 0,
            "active_client_id": active_client_id,
            "active_client": active,
            "active_client_summary": active_summary,
            "latest_error": latest_error,
            "processing": _active_processing,
            "server": {
                "pid": os.getpid(),
                "ws_host": WS_HOST,
                "ws_port": WS_PORT,
                "admin_host": ADMIN_HTTP_HOST,
                "admin_port": ADMIN_HTTP_PORT,
            },
            "modules": {
                "asr_enabled": _modules.asr_enabled,
                "llm_enabled": _modules.llm_enabled,
                "tts_enabled": _modules.tts_enabled,
            },
            "metrics": {
                "utterances_total": _metrics.utterances_total,
                "admin_tts_push_total": _metrics.admin_tts_push_total,
                "input_audio_bytes_total": _metrics.input_audio_bytes_total,
                "output_audio_bytes_total": _metrics.output_audio_bytes_total,
                "asr_ms_avg": round(_metrics.asr_ms_avg, 2),
                "llm_ms_avg": round(_metrics.llm_ms_avg, 2),
                "tts_ms_avg": round(_metrics.tts_ms_avg, 2),
                "total_ms_avg": round(_metrics.total_ms_avg, 2),
            },
        }
    return web.json_response(payload)


async def _admin_events(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 300))

    severity_filter_raw = request.query.get("severity", "error,warning")
    severity_filter = {
        part.strip()
        for part in severity_filter_raw.split(",")
        if part.strip() in {"debug", "info", "warning", "error"}
    }
    if not severity_filter:
        severity_filter = {"error", "warning"}

    async with _metrics_guard:
        rows = []
        for evt in reversed(_event_log):
            sev = _event_severity(evt["type"])
            if sev in severity_filter:
                row = dict(evt)
                row["severity"] = sev
                rows.append(row)
            if len(rows) >= limit:
                break
        rows.reverse()

    return web.json_response({"events": rows, "count": len(rows)})


async def _admin_connections(_request: web.Request) -> web.Response:
    async with _metrics_guard:
        active = _active_connection.to_dict() if _active_connection else None
        history = list(_connection_history[-50:])
    return web.json_response({"active": active, "history": history})


async def _admin_modules(request: web.Request) -> web.Response:
    data = await request.json()
    if "asr_enabled" in data:
        _modules.asr_enabled = bool(data["asr_enabled"])
    if "llm_enabled" in data:
        _modules.llm_enabled = bool(data["llm_enabled"])
    if "tts_enabled" in data:
        _modules.tts_enabled = bool(data["tts_enabled"])

    await _record_event(
        "modules_updated",
        asr_enabled=_modules.asr_enabled,
        llm_enabled=_modules.llm_enabled,
        tts_enabled=_modules.tts_enabled,
    )

    return web.json_response(
        {
            "ok": True,
            "updated_at": int(time.time()),
            "modules": {
                "asr_enabled": _modules.asr_enabled,
                "llm_enabled": _modules.llm_enabled,
                "tts_enabled": _modules.tts_enabled,
            },
        }
    )


async def _admin_send_text(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response(
            {"ok": False, "code": "BAD_JSON", "message": "Request body must be valid JSON."},
            status=400,
        )

    text = str(data.get("text", "")).strip()
    if not text:
        return web.json_response(
            {"ok": False, "code": "EMPTY_TEXT", "message": "Text payload is empty."},
            status=400,
        )

    async with _metrics_guard:
        ws = _active_ws
        session = _active_session
        connection = _active_connection
        processing = _active_processing
        tts_enabled = _modules.tts_enabled

    if not ws or not connection:
        return web.json_response(
            {"ok": False, "code": "NO_ACTIVE_CLIENT", "message": "No active device connection."},
            status=409,
        )

    if processing:
        return web.json_response(
            {"ok": False, "code": "CLIENT_BUSY", "message": "Client is processing an utterance."},
            status=409,
        )

    if not tts_enabled:
        return web.json_response(
            {"ok": False, "code": "TTS_DISABLED", "message": "TTS module is disabled by admin."},
            status=409,
        )

    try:
        audio_bytes, sample_rate = _synthesize_to_pcm16(text)
    except Exception as exc:
        await _record_event("error", code="ADMIN_TTS_ERROR", message=str(exc))
        return web.json_response(
            {"ok": False, "code": "ADMIN_TTS_ERROR", "message": str(exc)},
            status=500,
        )

    message_id = _next_admin_message_id()
    try:
        await _send_json(
            ws,
            {
                "type": "state",
                "state": "admin_tts",
                "message_id": message_id,
                "source": "admin_text",
            },
        )
        await _send_json(
            ws,
            {
                "type": "text",
                "utterance_id": 0,
                "asr_text": "",
                "reply_text": text,
                "sources": [],
                "web_route": "admin_text",
                "source": "admin_text",
                "message_id": message_id,
            },
        )
        await ws.send(audio_bytes)
        await _send_json(
            ws,
            {
                "type": "done",
                "utterance_id": 0,
                "encoding": "pcm16",
                "sample_rate": sample_rate,
                "channels": 1,
                "bytes": len(audio_bytes),
                "input_bytes": 0,
                "audio_disabled": False,
                "asr_ms": 0,
                "llm_ms": 0,
                "tts_ms": 0,
                "total_ms": 0,
                "assistant": ASSISTANT_NAME,
                "source": "admin_text",
                "message_id": message_id,
            },
        )
    except Exception as exc:
        await _record_event("error", code="ADMIN_SEND_FAILED", message=str(exc))
        return web.json_response(
            {"ok": False, "code": "ADMIN_SEND_FAILED", "message": str(exc)},
            status=500,
        )

    async with _metrics_guard:
        _metrics.admin_tts_push_total += 1
        _metrics.output_audio_bytes_total += len(audio_bytes)
        if _active_connection and _active_connection.client_id == connection.client_id:
            _active_connection.output_audio_bytes += len(audio_bytes)
            _active_connection.last_reply_text = _trim_text(text)
            _active_connection.last_route_reason = "admin_text"
        if session is not None:
            session.history.append(("assistant", f"[Admin Broadcast] {text}"))

    await _record_event(
        "admin_text_sent",
        message_id=message_id,
        client_id=connection.client_id,
        text_preview=_trim_text(text, 120),
        output_bytes=len(audio_bytes),
    )

    return web.json_response(
        {
            "ok": True,
            "message_id": message_id,
            "bytes": len(audio_bytes),
            "sample_rate": sample_rate,
            "updated_at": int(time.time()),
        }
    )


def _build_admin_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/admin", _admin_index)
    app.router.add_get("/api/admin/status", _admin_status)
    app.router.add_get("/api/admin/events", _admin_events)
    app.router.add_get("/api/admin/connections", _admin_connections)
    app.router.add_post("/api/admin/modules", _admin_modules)
    app.router.add_post("/api/admin/send-text", _admin_send_text)

    if _ADMIN_DIR.exists():
        app.router.add_static("/admin/", str(_ADMIN_DIR), show_index=False)
    return app


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
    print(f"Admin URL : http://{lan_ip}:{ADMIN_HTTP_PORT}/admin")
    print("=" * 60)

    admin_app = _build_admin_app()
    admin_runner = web.AppRunner(admin_app)
    await admin_runner.setup()
    admin_site = web.TCPSite(admin_runner, ADMIN_HTTP_HOST, ADMIN_HTTP_PORT)
    await admin_site.start()

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
