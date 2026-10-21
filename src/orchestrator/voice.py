"""Voice command recording and transcription service for Foreman Orchestrator.
Uses Linux system tools (arecord / ffmpeg) with zero heavy python dependencies.
Transcribes audio rapidly via Whisper / Gemini cloud endpoints.
"""

import base64
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Optional, Tuple


def get_recording_tool() -> Optional[str]:
    """Check if arecord or ffmpeg is available."""
    if shutil.which("arecord"):
        return "arecord"
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    return None


class VoiceRecorder:
    """Manages audio recording via arecord / ffmpeg subprocess."""

    def __init__(self, output_path: Optional[str] = None):
        self.output_path = output_path or f"/tmp/foreman_voice_{uuid.uuid4().hex[:6]}.wav"
        self.process: Optional[subprocess.Popen] = None
        self.is_recording = False
        self.tool = get_recording_tool()

    def start(self) -> bool:
        """Start capturing microphone audio."""
        if self.is_recording or not self.tool:
            return False

        if os.path.exists(self.output_path):
            try:
                os.remove(self.output_path)
            except Exception:
                pass

        try:
            if self.tool == "arecord":
                # CD Quality 16kHz mono WAV suitable for Whisper and speech models
                cmd = [
                    "arecord",
                    "-f", "S16_LE",
                    "-r", "16000",
                    "-c", "1",
                    "-t", "wav",
                    "-q",
                    self.output_path
                ]
            else:
                cmd = [
                    "ffmpeg",
                    "-f", "alsa",
                    "-i", "default",
                    "-ar", "16000",
                    "-ac", "1",
                    "-y",
                    "-loglevel", "quiet",
                    self.output_path
                ]

            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid
            )
            self.is_recording = True
            return True
        except Exception:
            self.is_recording = False
            return False

    def stop(self) -> Optional[str]:
        """Stop capturing and return the recorded WAV path if valid."""
        if not self.is_recording:
            return None

        self.is_recording = False
        if self.process:
            try:
                import signal
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                self.process.wait(timeout=1.5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None

        # Give disk a moment to flush WAV header
        time.sleep(0.1)

        if os.path.exists(self.output_path) and os.path.getsize(self.output_path) > 4000:
            return self.output_path
        return None


def _build_multipart_payload(fields: dict, files: dict, boundary: str) -> bytes:
    """Build a standard multipart/form-data payload in pure Python."""
    lines = []
    for key, value in fields.items():
        lines.append(f"--{boundary}".encode("utf-8"))
        lines.append(f'Content-Disposition: form-data; name="{key}"'.encode("utf-8"))
        lines.append(b"")
        lines.append(str(value).encode("utf-8"))

    for key, (filename, file_bytes, content_type) in files.items():
        lines.append(f"--{boundary}".encode("utf-8"))
        lines.append(f'Content-Disposition: form-data; name="{key}"; filename="{filename}"'.encode("utf-8"))
        lines.append(f"Content-Type: {content_type}".encode("utf-8"))
        lines.append(b"")
        lines.append(file_bytes)

    lines.append(f"--{boundary}--".encode("utf-8"))
    lines.append(b"")
    return b"\r\n".join(lines)


def transcribe_audio_groq(wav_path: str, api_key: str) -> Tuple[bool, str]:
    """Ultra-fast (0.2s) Whisper transcription via Groq."""
    try:
        with open(wav_path, "rb") as f:
            audio_bytes = f.read()

        boundary = f"ForemanVoiceBoundary{uuid.uuid4().hex}"
        fields = {
            "model": "whisper-large-v3-turbo",
            "response_format": "json",
            "temperature": "0.0"
        }
        files = {
            "file": ("audio.wav", audio_bytes, "audio/wav")
        }
        data = _build_multipart_payload(fields, files, boundary)

        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            res_obj = json.loads(resp.read().decode("utf-8"))
            return True, res_obj.get("text", "").strip()
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="replace")
        return False, f"Groq API error ({e.code}): {err_msg}"
    except Exception as e:
        return False, f"Transcription error: {str(e)}"


def transcribe_audio_openai(wav_path: str, api_key: str) -> Tuple[bool, str]:
    """Whisper transcription via OpenAI."""
    try:
        with open(wav_path, "rb") as f:
            audio_bytes = f.read()

        boundary = f"ForemanVoiceBoundary{uuid.uuid4().hex}"
        fields = {
            "model": "whisper-1",
            "response_format": "json"
        }
        files = {
            "file": ("audio.wav", audio_bytes, "audio/wav")
        }
        data = _build_multipart_payload(fields, files, boundary)

        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/transcriptions",
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            }
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            res_obj = json.loads(resp.read().decode("utf-8"))
            return True, res_obj.get("text", "").strip()
    except Exception as e:
        return False, f"OpenAI Whisper error: {str(e)}"


def transcribe_audio_gemini(wav_path: str, api_key: str) -> Tuple[bool, str]:
    """Fast multimodal audio transcription via Google Gemini 1.5 Flash."""
    try:
        with open(wav_path, "rb") as f:
            audio_b64 = base64.b64encode(f.read()).decode("utf-8")

        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": "Transcribe the spoken audio verbatim in the user's language. Output only the transcribed text, with no preamble, markdown, or quotation marks."
                        },
                        {
                            "inlineData": {
                                "mimeType": "audio/wav",
                                "data": audio_b64
                            }
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.0,
                "maxOutputTokens": 200
            }
        }

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
        data = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            }
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            res_obj = json.loads(resp.read().decode("utf-8"))
            candidates = res_obj.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    return True, parts[0].get("text", "").strip()
            return False, "No transcript generated by Gemini."
    except Exception as e:
        return False, f"Gemini Audio error: {str(e)}"


def get_configured_voice_key(preferred_key: Optional[str] = None) -> Optional[str]:
    """Retrieve voice API key from argument, environment, or providers.yaml."""
    if preferred_key:
        return preferred_key
    env_key = (
        os.environ.get("GROQ_API_KEY")
        or os.environ.get("GROQ_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    if env_key:
        return env_key
    for p_cand in [
        os.path.join(os.getcwd(), "providers.yaml"),
        os.path.expanduser("~/Desktop/Orcastrator/providers.yaml")
    ]:
        if os.path.exists(p_cand):
            try:
                import yaml
                with open(p_cand, "r", encoding="utf-8") as f:
                    cfg_data = yaml.safe_load(f) or {}
                k = cfg_data.get("voice", {}).get("api_key")
                if k:
                    return k
            except Exception:
                pass
    return None


def transcribe_audio(wav_path: str, preferred_key: Optional[str] = None) -> Tuple[bool, str]:
    """
    Intelligently transcribe WAV file using available API keys:
    1. Groq (if GROQ_API_KEY set or in providers.yaml -> 0.2s ultra-fast)
    2. OpenAI (if OPENAI_API_KEY set)
    3. Gemini (if GEMINI_API_KEY set)
    """
    active_key = get_configured_voice_key(preferred_key)
    if not active_key:
        return False, (
            "No Voice API Key configured.\n"
            "To enable ultra-fast voice transcription (0.2s):\n"
            "  • Run: export GROQ_API_KEY=gsk_... (Free & fastest at https://console.groq.com)\n"
            "  • Or:  export GEMINI_API_KEY=AIza... (Google Gemini Audio)\n"
            "  • Or:  /voice key <your_api_key>"
        )

    if not os.path.exists(wav_path):
        return False, f"Audio file not found: {wav_path}"

    if active_key.startswith("AIza"):
        return transcribe_audio_gemini(wav_path, active_key)
    elif active_key.startswith("sk-proj-"):
        return transcribe_audio_openai(wav_path, active_key)
    else:
        return transcribe_audio_groq(wav_path, active_key)
