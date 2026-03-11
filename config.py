"""
Centralized configuration for UNNC Campus Assistant.
Edit this file to adjust model paths, generation parameters, and behavior.
"""

import os

# ───────────────────────── Paths ─────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LLAMA_MODEL_PATH = r"C:\x\void\llm\llama.cpp\models\Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf"
KNOWLEDGE_BASE_PATH = os.path.join(BASE_DIR, "campus_info.txt")       # legacy single-file (still supported)
KNOWLEDGE_DIR = os.path.join(BASE_DIR, "knowledge")                    # multi-file KB directory (preferred)
LOG_DIR = os.path.join(BASE_DIR, "logs")

# ───────────────────────── LLM ───────────────────────────
LLM_N_CTX = 8192               # context window size
LLM_N_BATCH = 1024             # batch size for prompt processing
LLM_N_GPU_LAYERS = -1          # -1 = offload all layers to GPU

MAX_CONTEXT_TURNS = 20         # user-assistant turn pairs to keep
MAX_GENERATION_TOKENS = 1024
TEMPERATURE = 0.3              # lower = more deterministic (accuracy-first)
TOP_P = 0.9
REPEAT_PENALTY = 1.1           # penalize repetitive output

# ───────────────────────── ASR (Whisper) ─────────────────
WHISPER_MODEL_SIZE = "medium"   # tiny / base / small / medium / large
WHISPER_LANGUAGE = "en"         # force English; set None for auto-detect

# ───────────────────────── TTS ───────────────────────────
TTS_MODEL_NAME = "tts_models/en/vctk/vits"
TTS_SPEAKER = "p225"           # speaker ID within the VCTK model
TTS_SAMPLE_RATE = 22050        # fallback sample rate

# ───────────────────────── Voice Input ───────────────────
ENERGY_THRESHOLD = 1200        # mic sensitivity (higher = less sensitive)
DYNAMIC_ENERGY = False
PAUSE_THRESHOLD = 0.6          # seconds of silence before phrase end
PHRASE_TIME_LIMIT = 15         # max seconds per utterance

# ───────────────────────── Behavior ──────────────────────
RUN_MODE = "VOICE"               # "TEXT" or "VOICE"

# University identity
UNIVERSITY_NAME = "University of Nottingham Ningbo China"
UNIVERSITY_SHORT = "UNNC"
ASSISTANT_NAME = "Bluesee"

# ASR hallucination filter — phrases Whisper sometimes emits on silence
HALLUCINATION_PHRASES = [
    "thanks for watching",
    "thank you for watching",
    "thanks for listening",
    "thank you very much",
    "please subscribe",
    "like and subscribe",
]

# Emergency keywords — trigger immediate safety response
EMERGENCY_KEYWORDS = [
    "suicide", "kill myself", "self-harm", "hurt myself",
    "emergency", "fire", "help me", "i'm in danger",
    "心理危机", "自杀", "自残", "紧急",
]

# Knowledge retrieval — controls context budget
KB_TOP_K_SECTIONS = 5           # max sections to consider
KB_MAX_CONTEXT_CHARS = 8000     # hard cap on total KB context (~750 tokens)
KB_MIN_RELEVANCE_SCORE = 0.02   # drop sections below this absolute TF-IDF score
KB_RELATIVE_THRESHOLD = 0.4     # drop sections scoring < 40% of top section
KB_MAX_SECTION_CHARS = 1200     # sections larger than this are auto-chunked
KB_CHUNK_TARGET_CHARS = 800     # target size per chunk after splitting
KB_MAX_INJECT_CHARS = 800       # truncate individual sections at this limit

# Embedding retrieval (sentence-transformers)
EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"   # ~80 MB, fast, good quality
EMBEDDING_WEIGHT = 0.7                        # weight for embedding score in hybrid
TFIDF_WEIGHT = 0.3                            # weight for TF-IDF score in hybrid
