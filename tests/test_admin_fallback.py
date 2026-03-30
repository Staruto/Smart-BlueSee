"""Manual fallback tests for admin text-to-TTS push path.

Run this after ws_server.py is already running.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import urllib.error
import urllib.request

import websockets


def _http_post_json(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return int(resp.status), body
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8"))
        return int(exc.code), body


async def _expect_admin_tts_frames(ws, timeout: float = 10.0) -> dict:
    got_state = False
    got_text = False
    got_audio = False
    got_done = False
    done_payload: dict = {}

    while True:
        msg = await asyncio.wait_for(ws.recv(), timeout=timeout)
        if isinstance(msg, bytes):
            got_audio = len(msg) > 0
            continue

        parsed = json.loads(msg)
        if parsed.get("source") == "admin_text" and parsed.get("type") == "state":
            got_state = True
        if parsed.get("source") == "admin_text" and parsed.get("type") == "text":
            got_text = True
        if parsed.get("source") == "admin_text" and parsed.get("type") == "done":
            got_done = True
            done_payload = parsed
            break

    assert got_state, "Did not receive admin_text state frame"
    assert got_text, "Did not receive admin_text text frame"
    assert got_audio, "Did not receive admin_text audio bytes"
    assert got_done, "Did not receive admin_text done frame"
    return done_payload


async def run(args):
    send_url = args.admin_base.rstrip("/") + "/api/admin/send-text"
    module_url = args.admin_base.rstrip("/") + "/api/admin/modules"

    # 1) No active client -> NO_ACTIVE_CLIENT
    code, body = _http_post_json(send_url, {"text": "hello before ws"})
    assert code == 409 and body.get("code") == "NO_ACTIVE_CLIENT", (code, body)
    print("[PASS] NO_ACTIVE_CLIENT when no websocket connected")

    async with websockets.connect(args.ws_url, max_size=4 * 1024 * 1024) as ws:
        hello = await ws.recv()
        print("HELLO:", hello)

        # 2) Empty text -> EMPTY_TEXT
        code, body = _http_post_json(send_url, {"text": "   "})
        assert code == 400 and body.get("code") == "EMPTY_TEXT", (code, body)
        print("[PASS] EMPTY_TEXT validation")

        # 3) TTS disabled -> TTS_DISABLED
        code, body = _http_post_json(module_url, {"tts_enabled": False})
        assert code == 200 and body.get("ok") is True, (code, body)
        code, body = _http_post_json(send_url, {"text": "this should fail"})
        assert code == 409 and body.get("code") == "TTS_DISABLED", (code, body)
        print("[PASS] TTS_DISABLED when module switch is off")

        # Restore tts
        code, body = _http_post_json(module_url, {"tts_enabled": True})
        assert code == 200 and body.get("ok") is True, (code, body)

        # 4) Happy path
        code, body = _http_post_json(send_url, {"text": "Hello from admin fallback test."})
        assert code == 200 and body.get("ok") is True, (code, body)
        done = await _expect_admin_tts_frames(ws)
        assert done.get("source") == "admin_text", done
        print("[PASS] admin_text TTS push happy path")

    print("All admin fallback checks passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ws-url", default="ws://127.0.0.1:8765")
    parser.add_argument("--admin-base", default="http://127.0.0.1:8766")
    args = parser.parse_args()

    asyncio.run(run(args))
