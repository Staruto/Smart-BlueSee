"""Simple loopback tester for ws_server.py protocol v1."""

from __future__ import annotations

import argparse
import asyncio
import json

import numpy as np
import websockets


def _make_pcm16_sine(sample_rate: int, seconds: float, hz: float = 440.0) -> bytes:
    t = np.linspace(0, seconds, int(sample_rate * seconds), endpoint=False)
    y = 0.2 * np.sin(2.0 * np.pi * hz * t)
    return (y * 32767).astype(np.int16).tobytes()


async def run_once(url: str, sample_rate: int, seconds: float):
    audio = _make_pcm16_sine(sample_rate=sample_rate, seconds=seconds)

    async with websockets.connect(url, max_size=4 * 1024 * 1024) as ws:
        hello = await ws.recv()
        print("HELLO:", hello)

        await ws.send(json.dumps({"type": "start_utterance"}))
        print("START ACK:", await ws.recv())

        chunk_size = 640  # 20ms for PCM16 mono @ 16kHz
        for i in range(0, len(audio), chunk_size):
            await ws.send(audio[i : i + chunk_size])

        await ws.send(json.dumps({"type": "end_utterance"}))

        # Read until done/error/ignored
        while True:
            msg = await ws.recv()
            if isinstance(msg, bytes):
                print(f"AUDIO BYTES: {len(msg)}")
                continue

            print("JSON:", msg)
            parsed = json.loads(msg)
            if parsed.get("type") in {"done", "error", "ignored"}:
                break


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="ws://127.0.0.1:8765")
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--seconds", type=float, default=1.0)
    args = parser.parse_args()

    asyncio.run(run_once(args.url, args.sample_rate, args.seconds))
