"""
UNNC Campus Assistant — client v4
==================================
Improvements over v3
--------------------
1.  Modular config (config.py) — all tunables in one place.
2.  Section-based KB retrieval (knowledge_base.py) — only relevant
    sections injected into the prompt, saving context tokens.
3.  System prompt aligned to UNNC / "Bluesee" identity.
4.  Emergency-intent detection — safety keywords trigger immediate
    professional-help guidance before LLM generation.
5.  User commands:  /help  /reset  /history  /mode  /reload  /exit
6.  Logs written to logs/ directory.
7.  Duplicate `chat_history` declaration removed.
8.  Generation params (temperature, top_p, repeat_penalty) exposed.
9.  Async TTS pipeline — synthesis runs in a background thread so
    token streaming is not blocked.
10. Conversation summary — when history exceeds threshold, older
    turns are condensed into a summary to keep the prompt compact.
"""

import os
import sys
import tempfile
import datetime
import threading
import queue
import re
import contextlib
import io

import numpy as np
import pyaudio
import whisper
import speech_recognition as sr
from llama_cpp import Llama
from TTS.api import TTS

from config import (
    LLAMA_MODEL_PATH, LOG_DIR,
    LLM_N_CTX, LLM_N_BATCH, LLM_N_GPU_LAYERS,
    MAX_CONTEXT_TURNS, MAX_GENERATION_TOKENS,
    TEMPERATURE, TOP_P, REPEAT_PENALTY,
    WHISPER_MODEL_SIZE, WHISPER_LANGUAGE,
    TTS_MODEL_NAME, TTS_SPEAKER, TTS_SAMPLE_RATE,
    ENERGY_THRESHOLD, DYNAMIC_ENERGY, PAUSE_THRESHOLD, PHRASE_TIME_LIMIT,
    RUN_MODE,
    UNIVERSITY_NAME, UNIVERSITY_SHORT, ASSISTANT_NAME,
    HALLUCINATION_PHRASES, EMERGENCY_KEYWORDS,
)
from knowledge_base import KnowledgeBase


# ═══════════════════════════════════════════════════════════
#  Ensure log directory
# ═══════════════════════════════════════════════════════════
os.makedirs(LOG_DIR, exist_ok=True)


# ═══════════════════════════════════════════════════════════
#  Audio Player  (unchanged, but wrapped for clarity)
# ═══════════════════════════════════════════════════════════
class AudioPlayer:
    """Threaded audio playback queue using PyAudio."""

    def __init__(self, sample_rate: int = TTS_SAMPLE_RATE):
        self.p = pyaudio.PyAudio()
        self.queue: queue.Queue = queue.Queue()
        self.running = True
        self.sample_rate = sample_rate
        self.stream = self.p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self.sample_rate,
            output=True,
        )
        self.thread = threading.Thread(target=self._play_loop, daemon=True)
        self.thread.start()

    def _play_loop(self):
        while self.running or not self.queue.empty():
            try:
                data = self.queue.get(timeout=0.1)
                self.stream.write(data)
                self.queue.task_done()
            except queue.Empty:
                continue

    def add_audio(self, audio_data: bytes):
        self.queue.put(audio_data)

    def stop(self):
        self.queue.join()
        self.running = False
        self.thread.join()
        self.stream.stop_stream()
        self.stream.close()
        self.p.terminate()


# ═══════════════════════════════════════════════════════════
#  Load Models
# ═══════════════════════════════════════════════════════════
print("[System]: Loading knowledge base...")
kb = KnowledgeBase()

print("[System]: Loading Whisper ASR model...")
asr_model = whisper.load_model(WHISPER_MODEL_SIZE)

print("[System]: Loading LLM...")
llm = Llama(
    model_path=LLAMA_MODEL_PATH,
    n_ctx=LLM_N_CTX,
    n_batch=LLM_N_BATCH,
    n_gpu_layers=LLM_N_GPU_LAYERS,
    verbose=False,
)

print("[System]: Loading TTS model...")
tts_engine = TTS(model_name=TTS_MODEL_NAME, progress_bar=False, gpu=True)


# ═══════════════════════════════════════════════════════════
#  Conversation State
# ═══════════════════════════════════════════════════════════
chat_history: list[tuple[str, str]] = []       # (role, content)
conversation_summary: str = ""                  # compressed older turns
current_run_mode: str = RUN_MODE                # mutable at runtime


# ═══════════════════════════════════════════════════════════
#  System Prompt
# ═══════════════════════════════════════════════════════════
def get_system_prompt(context: str) -> str:
    """Build Llama-3.1 system prompt with relevant context."""
    return (
        "<|start_header_id|>system<|end_header_id|>\n"
        f"You are '{ASSISTANT_NAME}', the AI campus assistant for "
        f"{UNIVERSITY_NAME} ({UNIVERSITY_SHORT}).\n"
        "Your goal is to help students and faculty with daily campus needs.\n"
        "\n"
        "CONTEXT INFORMATION:\n"
        f"{context}\n"
        "\n"
        "INSTRUCTIONS:\n"
        "1. ONLY use the Context Information above to answer campus-related "
        "questions. Do NOT add, invent, or guess any information that is "
        "not explicitly stated in the context — including program names, "
        "locations, phone numbers, policies, or dates. If the answer is "
        "not in the context, say so and suggest contacting The Hub "
        "(Portland Building 120) or the relevant office.\n"
        "2. When listing items (programs, services, etc.), list ONLY "
        "those that appear in the Context Information. Never pad the "
        "list with items from your general knowledge.\n"
        "3. Be concise, accurate, and friendly.\n"
        "4. For mental-health or safety concerns, ALWAYS prioritize "
        "suggesting professional help: Campus Counseling Center and the "
        "24-hour Emergency Hotline (8818 0000).\n"
        "5. You may answer general (non-campus) questions briefly, but "
        "always clarify when information is outside your campus knowledge.\n"
        "6. When users greet you, introduce yourself as Bluesee, the UNNC "
        "campus assistant.\n"
        "<|eot_id|>\n"
    )


# ═══════════════════════════════════════════════════════════
#  Emergency Detection
# ═══════════════════════════════════════════════════════════
def check_emergency(text: str) -> str | None:
    """
    If the user text contains emergency keywords, return an immediate
    safety message. Otherwise return None.
    """
    t = text.lower()
    for kw in EMERGENCY_KEYWORDS:
        if kw in t:
            return (
                "I'm concerned about your safety. Please reach out to "
                "professional help immediately:\n"
                "• Campus Counseling Center (The Hub, Portland 120)\n"
                "• 24-hour Emergency Hotline: 8818 0000\n"
                "• National Crisis Hotline (China): 400-161-9995\n\n"
                "You are not alone — trained professionals are here to help."
            )
    return None


# ═══════════════════════════════════════════════════════════
#  ASR Hallucination Filter
# ═══════════════════════════════════════════════════════════
def should_ignore(text: str) -> bool:
    t = text.lower().strip()
    if len(t.split()) < 2:
        return True
    return any(p in t for p in HALLUCINATION_PHRASES)


# ═══════════════════════════════════════════════════════════
#  Conversation Summarization
# ═══════════════════════════════════════════════════════════
SUMMARIZE_THRESHOLD = MAX_CONTEXT_TURNS * 2  # number of entries before summarizing

def maybe_summarize():
    """
    When chat_history grows beyond the threshold, compress the oldest
    half into a textual summary so the prompt stays compact.
    """
    global conversation_summary

    if len(chat_history) <= SUMMARIZE_THRESHOLD:
        return

    # Take the oldest half
    cutoff = len(chat_history) // 2
    old_turns = chat_history[:cutoff]

    # Build a short summary string
    summary_lines = []
    for role, content in old_turns:
        tag = "User" if role == "user" else "Assistant"
        # Truncate long messages to first 120 chars
        short = content[:120].replace("\n", " ")
        if len(content) > 120:
            short += "..."
        summary_lines.append(f"- {tag}: {short}")

    new_summary = "\n".join(summary_lines)
    if conversation_summary:
        conversation_summary += "\n" + new_summary
    else:
        conversation_summary = new_summary

    # Remove those entries from history
    del chat_history[:cutoff]
    print(f"[System]: Summarized {cutoff} older messages to save context space.")


# ═══════════════════════════════════════════════════════════
#  Prompt Builder
# ═══════════════════════════════════════════════════════════
def build_prompt(user_input: str) -> str:
    """
    Full prompt:  system → (summary) → history → current user input.
    Context is retrieved from the KB based on the query.
    Returns the prompt string.  Also prints retrieval stats.
    """
    # Retrieve only relevant KB sections (with stats)
    context, kb_stats = kb.retrieve_debug(user_input)
    system_prompt = get_system_prompt(context)

    n = kb_stats.get("sections_used", "?")
    c = kb_stats.get("context_chars", "?")
    t = kb_stats.get("context_tokens_est", "?")
    print(f"[KB]: {n} sections, {c} chars (~{t} tokens)")
    if kb_stats.get("details"):
        for sc, title, src, cost in kb_stats["details"]:
            print(f"       {sc:.3f}  [{title}] ({src}) {cost}ch")

    history_block = ""

    # Inject conversation summary if it exists
    if conversation_summary:
        history_block += (
            "<|start_header_id|>system<|end_header_id|>\n"
            "Summary of earlier conversation:\n"
            f"{conversation_summary}\n"
            "<|eot_id|>\n"
        )

    for role, content in chat_history[-MAX_CONTEXT_TURNS * 2:]:
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


# ═══════════════════════════════════════════════════════════
#  Async TTS Pipeline
# ═══════════════════════════════════════════════════════════
class AsyncTTSPipeline:
    """
    Accepts text chunks and synthesizes audio in a background thread,
    feeding results to an AudioPlayer.  This avoids blocking the
    token-streaming loop.
    """

    def __init__(self, player: AudioPlayer):
        self.player = player
        self._text_queue: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def _worker(self):
        while True:
            chunk = self._text_queue.get()
            if chunk is None:  # poison pill
                self._text_queue.task_done()
                break
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    wav = tts_engine.tts(text=chunk, speaker=TTS_SPEAKER)
                wav_np = np.array(wav, dtype=np.float32)
                audio_int16 = (wav_np * 32767).clip(-32768, 32767).astype(np.int16)
                self.player.add_audio(audio_int16.tobytes())
            except Exception as e:
                print(f"[TTS Error]: {e}")
            self._text_queue.task_done()

    def synthesize(self, text: str):
        if text.strip():
            self._text_queue.put(text)

    def finish(self):
        """Signal no more text and wait for synthesis to complete."""
        self._text_queue.put(None)
        self._text_queue.join()


# ═══════════════════════════════════════════════════════════
#  Core Processing
# ═══════════════════════════════════════════════════════════

def process_user_text(user_text: str, log_file: str) -> str:
    """Process one user message: LLM generate + optional TTS."""
    print(f"\n[You]: {user_text}")

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"\n[You]: {user_text}\n")

    # ── Emergency check ──
    emergency_msg = check_emergency(user_text)
    if emergency_msg:
        print(f"[{ASSISTANT_NAME}]: {emergency_msg}\n")
        chat_history.append(("user", user_text))
        chat_history.append(("assistant", emergency_msg))
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{ASSISTANT_NAME}]: {emergency_msg}\n")
        return emergency_msg

    # ── Summarize if needed ──
    maybe_summarize()

    prompt = build_prompt(user_text)

    print(f"[{ASSISTANT_NAME}]: ", end="", flush=True)
    full_response = ""

    # ── TTS setup ──
    player = None
    tts_pipeline = None
    if current_run_mode == "VOICE":
        try:
            sample_rate = getattr(tts_engine.synthesizer, "output_sample_rate", TTS_SAMPLE_RATE)
            player = AudioPlayer(sample_rate=sample_rate)
            tts_pipeline = AsyncTTSPipeline(player)
        except Exception as e:
            print(f"\n[TTS Init Error]: {e}")

    buffer = ""
    sentence_end_pattern = re.compile(r"(?<=[.!?。！？])\s*")

    # ── Stream LLM output ──
    stream = llm(
        prompt,
        max_tokens=MAX_GENERATION_TOKENS,
        stop=["<|eot_id|>"],
        temperature=TEMPERATURE,
        top_p=TOP_P,
        repeat_penalty=REPEAT_PENALTY,
        stream=True,
    )

    for chunk in stream:
        token = chunk["choices"][0]["text"]
        print(token, end="", flush=True)
        full_response += token

        if tts_pipeline:
            buffer += token
            parts = sentence_end_pattern.split(buffer)
            if len(parts) > 1:
                tts_pipeline.synthesize(parts[0])
                buffer = "".join(parts[1:])

    print("\n")

    # ── Flush remaining TTS buffer ──
    if tts_pipeline:
        if buffer.strip():
            tts_pipeline.synthesize(buffer)
        tts_pipeline.finish()

    if player:
        player.stop()

    full_response = full_response.strip()

    # ── Update history ──
    chat_history.append(("user", user_text))
    chat_history.append(("assistant", full_response))

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"[{ASSISTANT_NAME}]: {full_response}\n")

    # ── Stats ──
    prompt_tokens = llm.tokenize(prompt.encode("utf-8"))
    print(f"[Prompt]: {len(prompt_tokens)} tokens | {len(prompt)} chars\n")

    return full_response


# ═══════════════════════════════════════════════════════════
#  User Commands
# ═══════════════════════════════════════════════════════════

COMMAND_HELP = """
Available commands:
  /help      — Show this help message
  /reset     — Clear conversation history
  /history   — Show recent conversation turns
  /mode      — Toggle between TEXT and VOICE mode
  /reload    — Reload the knowledge base from disk
  /stats     — Show model and session statistics
  /kb <query> — Debug: show which KB sections match a query
  /exit      — Exit the program
"""


def handle_command(cmd: str, log_file: str) -> bool:
    """
    Handle slash-commands.  Returns True if the input was a command
    (and was handled), False if it's regular text.
    """
    global current_run_mode, conversation_summary

    cmd_lower = cmd.strip().lower()

    if not cmd_lower.startswith("/"):
        return False

    if cmd_lower == "/help":
        print(COMMAND_HELP)

    elif cmd_lower == "/reset":
        chat_history.clear()
        conversation_summary = ""
        print("[System]: Conversation history cleared.\n")

    elif cmd_lower == "/history":
        if not chat_history:
            print("[System]: No conversation history yet.\n")
        else:
            print("\n--- Recent History ---")
            for role, content in chat_history[-10:]:
                tag = "You" if role == "user" else ASSISTANT_NAME
                short = content[:200].replace("\n", " ")
                if len(content) > 200:
                    short += "..."
                print(f"  [{tag}]: {short}")
            print("--- End ---\n")

    elif cmd_lower == "/mode":
        if current_run_mode == "TEXT":
            current_run_mode = "VOICE"
        else:
            current_run_mode = "TEXT"
        print(f"[System]: Switched to {current_run_mode} mode.\n")

    elif cmd_lower == "/reload":
        kb.load()
        print(f"[System]: Knowledge base reloaded — "
              f"{len(kb.sections)} sections from {kb.file_count} file(s).\n")

    elif cmd_lower == "/stats":
        print(f"  Model       : {os.path.basename(LLAMA_MODEL_PATH)}")
        print(f"  Context     : {LLM_N_CTX} tokens")
        print(f"  History     : {len(chat_history)} entries ({len(chat_history)//2} turns)")
        print(f"  KB          : {len(kb.sections)} sections from {kb.file_count} file(s)")
        print(f"  Mode        : {current_run_mode}")
        print(f"  Temperature : {TEMPERATURE}")
        print()

    elif cmd_lower.startswith("/kb "):
        query = cmd.strip()[4:]
        if query:
            kb.search_debug(query)
        else:
            print("[System]: Usage: /kb <query>")
        print()

    elif cmd_lower in ("/exit", "/quit"):
        print("[System]: Exiting program.")
        sys.exit(0)

    else:
        print(f"[System]: Unknown command '{cmd_lower}'. Type /help for options.\n")

    return True


# ═══════════════════════════════════════════════════════════
#  Main Loops
# ═══════════════════════════════════════════════════════════

def text_chat_loop():
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(LOG_DIR, f"chat_log_{timestamp}.txt")

    with open(log_file, "w", encoding="utf-8") as f:
        f.write(f"=== Text Conversation Log ({timestamp}) ===\n")

    print("\n" + "=" * 60)
    print(f"  {ASSISTANT_NAME} — {UNIVERSITY_SHORT} Campus Assistant")
    print("  Text chat mode.  Type /help for commands.")
    print("  Press Ctrl+C to exit.")
    print("=" * 60 + "\n")

    while True:
        try:
            user_text = input("> ").strip()
            if not user_text:
                continue

            if handle_command(user_text, log_file):
                continue

            process_user_text(user_text, log_file)

        except KeyboardInterrupt:
            print("\n[System]: Exiting program.")
            break


def listen_and_process():
    recognizer = sr.Recognizer()
    recognizer.energy_threshold = ENERGY_THRESHOLD
    recognizer.dynamic_energy_threshold = DYNAMIC_ENERGY
    recognizer.pause_threshold = PAUSE_THRESHOLD

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(LOG_DIR, f"chat_log_{timestamp}.txt")

    with open(log_file, "w", encoding="utf-8") as f:
        f.write(f"=== Voice Conversation Log ({timestamp}) ===\n")

    print("\n" + "=" * 60)
    print(f"  {ASSISTANT_NAME} — {UNIVERSITY_SHORT} Campus Assistant")
    print("  Voice mode.  Speak into the microphone.")
    print("  Press Ctrl+C to exit.")
    print(f"  Log: {log_file}")
    print("=" * 60 + "\n")

    while True:
        try:
            with sr.Microphone() as source:
                recognizer.adjust_for_ambient_noise(source, duration=0.5)
                print("[State]: Listening...", end="\r")

                audio_data = recognizer.listen(
                    source,
                    timeout=None,
                    phrase_time_limit=PHRASE_TIME_LIMIT,
                )

            print("[State]: Transcribing...", end="\r")

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio_data.get_wav_data())
                temp_wav = f.name

            result = asr_model.transcribe(
                temp_wav,
                fp16=False,
                language=WHISPER_LANGUAGE,
            )

            os.remove(temp_wav)

            user_text = result["text"].strip()

            if should_ignore(user_text):
                print("[ASR]: Ignored noisy input")
                continue

            process_user_text(user_text, log_file)

        except KeyboardInterrupt:
            print("\n[System]: Exiting program.")
            break
        except Exception as e:
            print(f"\n[Error]: {e}")


# ═══════════════════════════════════════════════════════════
#  Entry Point
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    if current_run_mode == "TEXT":
        text_chat_loop()
    else:
        listen_and_process()
