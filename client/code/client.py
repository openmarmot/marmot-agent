#!/usr/bin/env python3
"""
Marmot Agent Client

- Hold Right Option/Alt to record -> send audio to local Marmot server /connect
- Server does STT + LLM (tools) + TTS
- Client receives transcription + AI response + audio
- Prints "You:" (transcription) then "Marmot:" reply, plays audio, copies reply to clipboard
- -m "text" flag: send text directly, play/print/copy response, exit (for testing)
"""

import os
import sys
import tempfile
import time
import threading
import signal
import argparse
import sounddevice as sd
import requests
import subprocess
import numpy as np
from pynput import keyboard
import wave
import platform
import json
import base64

# ========================= CONFIG =========================
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "client_config.json")
HOTKEY = keyboard.Key.alt_r  # Right Option (⌥) on macOS / Right Alt on Win/Linux

def _fix_url(u):
    u = (u or "").strip()
    if u and not u.startswith(("http://", "https://")):
        u = "http://" + u
    return u.rstrip("/")

def load_client_config():
    cfg = {
        "GAIN": 4.0,
        "MARMOT_SERVER": None,
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                loaded = json.load(f)
            cfg.update({k: v for k, v in loaded.items() if k in cfg})
        except Exception:
            pass

    needs_save = False
    if not cfg.get("MARMOT_SERVER"):
        srv = input("\nEnter Marmot server address (host:port) [default: localhost:5000]: ").strip()
        if not srv:
            srv = "localhost:5000"
        cfg["MARMOT_SERVER"] = srv
        needs_save = True

    if needs_save:
        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump(cfg, f, indent=2)
            print(f"✅ Saved config to {CONFIG_PATH}")
        except Exception as e:
            print("⚠️  Could not save config:", e)
    return cfg

config = load_client_config()
GAIN = float(config.get("GAIN", 4.0))
MARMOT_SERVER = _fix_url(config.get("MARMOT_SERVER", "localhost:5000"))
if not MARMOT_SERVER.startswith("http"):
    MARMOT_BASE = f"http://{MARMOT_SERVER}"
else:
    MARMOT_BASE = MARMOT_SERVER

print(f"🐹 Marmot Agent client")
print(f"   Server: {MARMOT_BASE}/connect")
print(f"   Gain:   {GAIN}x")
print()

# ====================== AUDIO PLAYBACK ======================
def play_wav(path):
    """Play WAV using sounddevice (cross-platform, reuses deps)."""
    try:
        with wave.open(path, 'rb') as wf:
            sr = wf.getframerate()
            nch = wf.getnchannels()
            sw = wf.getsampwidth()
            frames = wf.readframes(wf.getnframes())
            if sw == 2:
                audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
            elif sw == 1:
                audio = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128) / 128.0
            elif sw == 4:
                audio = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
            else:
                audio = np.frombuffer(frames, dtype=np.float32)
            if nch > 1:
                audio = audio.reshape(-1, nch)
            sd.play(audio, samplerate=sr)
            sd.wait()
        print("🔊 Playback done")
    except Exception as e:
        print("Playback error:", e)

# ====================== CLIPBOARD ======================
SYSTEM = platform.system()

def copy_to_clipboard(text):
    if not text:
        return
    try:
        if SYSTEM == "Darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        elif SYSTEM == "Windows":
            subprocess.run(["clip"], input=text.encode("utf-8"), check=True)
        elif SYSTEM == "Linux":
            try:
                subprocess.run(["wl-copy"], input=text.encode("utf-8"), check=True)
            except FileNotFoundError:
                subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode("utf-8"), check=True)
        print("📋 Copied to clipboard")
    except Exception as e:
        print(f"Clipboard failed ({SYSTEM}): {e}")

# ====================== SEND TO SERVER ======================
def send_to_marmot(audio_path=None, text=None):
    url = f"{MARMOT_BASE}/connect"
    try:
        if audio_path and os.path.exists(audio_path):
            print("📤 Sending audio to Marmot server...")
            with open(audio_path, "rb") as f:
                files = {"file": f}
                resp = requests.post(url, files=files, timeout=300)
        else:
            print(f"📤 Sending text: {text[:80]}{'...' if text and len(text)>80 else ''}")
            resp = requests.post(url, json={"text": text or ""}, timeout=300)

        if resp.status_code != 200:
            print(f"Server error {resp.status_code}: {resp.text[:200]}")
            return None, None, None

        data = resp.json()
        transcription = data.get("transcription", "")
        resp_text = data.get("text", "")
        audio_b64 = data.get("audio")
        return transcription, resp_text, audio_b64
    except Exception as e:
        print("Send failed:", e)
        return None, None, None

def handle_response(transcription, resp_text, audio_b64):
    if resp_text is None:
        return

    if transcription:
        print(f"🗣️  You: {transcription}")

    print(f"🐹 Marmot: {resp_text}\n")
    copy_to_clipboard(resp_text)
    if audio_b64:
        try:
            audio_bytes = base64.b64decode(audio_b64)
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name
            try:
                play_wav(tmp_path)
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
        except Exception as e:
            print("Audio decode/play error:", e)
    else:
        print("(no audio returned)")

# ====================== RECORDING (borrowed from spark-dictate) ======================
recording = False
audio_data = []
stream = None
lock = threading.Lock()

def callback(indata, frames, time_info, status):
    if status:
        print("Audio status:", status)
    with lock:
        audio_data.append(indata.copy())

def start_recording():
    global stream, audio_data, recording
    with lock:
        audio_data = []
        recording = True
    print("🎤 Recording... (hold Right ⌥ / Alt)")
    try:
        stream = sd.InputStream(samplerate=16000, channels=1, dtype="float32", callback=callback)
        stream.start()
    except Exception as e:
        print("Mic start failed:", e)
        recording = False

def stop_recording():
    global stream, recording
    print("⏹️  Stopping...")
    with lock:
        recording = False
    if stream:
        stream.stop()
        stream.close()
        stream = None
    process_and_send()

def process_and_send():
    global audio_data
    if not audio_data:
        print("No audio captured")
        return

    arr = np.concatenate(audio_data, axis=0).flatten()
    peak = np.max(np.abs(arr))
    print(f"🔊 Peak: {peak:.4f}")

    boosted = (arr * GAIN).clip(-1.0, 1.0)
    # 0.5s silence pad front+back like spark
    silence = np.zeros(int(16000 * 0.5), dtype=np.int16)
    pcm = (boosted * 32767).astype(np.int16)
    padded = np.concatenate([silence, pcm, silence])

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(padded.tobytes())

    try:
        transcription, resp_text, audio_b64 = send_to_marmot(audio_path=tmp_path)
        handle_response(transcription, resp_text, audio_b64)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

# ====================== HOTKEY ======================
def on_press(key):
    global recording
    if key == HOTKEY and not recording:
        threading.Thread(target=start_recording, daemon=True).start()

def on_release(key):
    global recording
    if key == HOTKEY and recording:
        threading.Thread(target=stop_recording, daemon=True).start()

def signal_handler(sig, frame):
    print("\n👋 Shutting down...")
    if stream:
        stream.stop()
        stream.close()
    os._exit(0)

# ====================== TEXT MESSAGE MODE (-m) ======================
def send_message_mode(message: str):
    print(f"🐹 Sending message: {message}")
    transcription, resp_text, audio_b64 = send_to_marmot(text=message)
    handle_response(transcription, resp_text, audio_b64)
    print("Done.")

# ====================== MAIN ======================
def main():
    parser = argparse.ArgumentParser(description="Marmot Agent Client")
    parser.add_argument("-m", "--message", type=str, help="Send text message, play/print response, then exit")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, signal_handler)

    if args.message:
        send_message_mode(args.message)
        return

    print("   Hold Right Option (⌥) / Right Alt to speak → release for AI response")
    print("   Use -m \"your text\" for quick text queries (no recording)")
    print()

    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        try:
            listener.join()
        except KeyboardInterrupt:
            signal_handler(None, None)

if __name__ == "__main__":
    main()
