"""Unit tests for Voice command module and new message container UI."""

import os
import unittest
from unittest.mock import patch, MagicMock
from orchestrator.voice import VoiceRecorder, transcribe_audio, get_recording_tool
from orchestrator.markdown import render_user_message, render_assistant_response, render_markdown


class TestVoiceModule(unittest.TestCase):
    """Test voice recorder and transcription handling."""

    def test_recording_tool_detection(self):
        """Verify arecord or ffmpeg detection."""
        tool = get_recording_tool()
        self.assertIn(tool, ["arecord", "ffmpeg", None])

    def test_voice_recorder_init(self):
        """Test recorder initialization."""
        rec = VoiceRecorder(output_path="/tmp/test_rec.wav")
        self.assertEqual(rec.output_path, "/tmp/test_rec.wav")
        self.assertFalse(rec.is_recording)

    @patch("orchestrator.voice.get_configured_voice_key", return_value=None)
    def test_transcribe_audio_no_key_returns_instructions(self, mock_key):
        """Verify helpful setup instructions returned when no key is configured."""
        ok, msg = transcribe_audio("/tmp/fake.wav")
        self.assertFalse(ok)
        self.assertIn("No Voice API Key configured", msg)
        self.assertIn("GROQ_API_KEY", msg)

    @patch("urllib.request.urlopen")
    def test_transcribe_audio_groq_mock(self, mock_urlopen):
        """Test Groq Whisper transcription flow with mock response."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"text": "run that project"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(b"RIFF....WAVEfmt ....data....")
            temp_wav = f.name

        try:
            ok, text = transcribe_audio(temp_wav, preferred_key="gsk_mock_test_key_123")
            self.assertTrue(ok)
            self.assertEqual(text, "run that project")
        finally:
            if os.path.exists(temp_wav):
                os.remove(temp_wav)

    def test_voice_recording_animation_frame(self):
        """Verify format_voice_recording_frame outputs animated equalizer, timer, and indicator."""
        from orchestrator.voice import format_voice_recording_frame, AUDIO_WAVE_FRAMES, SPINNER_FRAMES, REC_PULSE_FRAMES

        self.assertGreater(len(AUDIO_WAVE_FRAMES), 0)
        self.assertGreater(len(SPINNER_FRAMES), 0)
        self.assertGreater(len(REC_PULSE_FRAMES), 0)

        frame0 = format_voice_recording_frame(0, 0.0)
        self.assertIn("🎙️", frame0)
        self.assertIn("REC", frame0)
        self.assertIn("(0.0s)", frame0)
        self.assertIn("Listening...", frame0)

        frame5 = format_voice_recording_frame(5, 2.5)
        self.assertIn("(2.5s)", frame5)
        self.assertIn("Listening...", frame5)
        self.assertIn("\r\033[K", frame5)


class TestMessageContainersUI(unittest.TestCase):
    """Test clean UI section containers for user and assistant."""

    def test_render_user_message(self):
        """Test user message box formatting."""
        card = render_user_message("build authentication module", width=80)
        self.assertIn("👤 You", card)
        self.assertIn("build authentication module", card)
        self.assertIn("╭─", card)
        self.assertIn("╰─", card)

    def test_render_assistant_response(self):
        """Test assistant response box formatting with duration."""
        card = render_assistant_response("All 4 tasks completed.", provider="antigravity_cli", duration_sec=12.5, width=80)
        self.assertIn("🤖 Foreman (antigravity_cli)", card)
        self.assertIn("All 4 tasks completed.", card)
        self.assertIn("Responded in 12.5s", card)
        self.assertIn("╭─", card)
        self.assertIn("╰─", card)

    def test_ansi_orphan_sanitization(self):
        """Test that malformed or orphaned ANSI codes like 1;37m are cleanly sanitized."""
        dirty = "• 1;37mDev Server:\x1b[0m Active at http://localhost:5173/"
        cleaned = render_markdown(dirty)
        self.assertNotIn("1;37m", cleaned)
        self.assertIn("Dev Server:", cleaned)


if __name__ == "__main__":
    unittest.main()
