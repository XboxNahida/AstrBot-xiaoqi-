import os
import sys
import time
import uuid
import glob
import base64
import asyncio
import tempfile
import threading
import subprocess
from typing import Optional
import edge_tts

try:
    import requests
except ImportError:
    requests = None

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

if IS_WIN:
    import ctypes
    winmm = ctypes.windll.winmm
else:
    winmm = None

class EdgeSpeaker:
    def __init__(
        self,
        voice: str = "zh-CN-XiaoyiNeural",
        rate: str = "+10%",
        pitch: str = "+10Hz",
        enabled: bool = True,
        engine_type: str = "edge_tts",
        api_url: str = "",
        api_key: str = "",
        model: str = "",
        gemini_api_key: str = "",
        gemini_voice: str = "Aoede",
        gemini_model: str = "gemini-3.8-flash-tts"
    ):
        self.voice = voice or "zh-CN-XiaoyiNeural"
        self.rate = rate or "+10%"
        self.pitch = pitch or "+10Hz"
        self.enabled = enabled
        self.engine_type = engine_type or "edge_tts"
        self.api_url = (api_url or "").strip()
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.gemini_api_key = (gemini_api_key or "").strip()
        self.gemini_voice = (gemini_voice or "Aoede").strip()
        self.gemini_model = (gemini_model or "gemini-3.8-flash-tts").strip()

        self._temp_dir = os.path.join(tempfile.gettempdir(), "gemini_pet_tts")
        os.makedirs(self._temp_dir, exist_ok=True)
        self._current_alias = "gemini_voice"
        self._lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._cancel_generation = 0
        self._current_process: Optional[subprocess.Popen] = None

    def update_config(
        self,
        voice: Optional[str] = None,
        rate: Optional[str] = None,
        pitch: Optional[str] = None,
        enabled: Optional[bool] = None,
        engine_type: Optional[str] = None,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        gemini_api_key: Optional[str] = None,
        gemini_voice: Optional[str] = None,
        gemini_model: Optional[str] = None
    ):
        if voice is not None:
            self.voice = voice
        if rate is not None:
            self.rate = rate
        if pitch is not None:
            self.pitch = pitch
        if enabled is not None:
            self.enabled = enabled
        if engine_type is not None:
            self.engine_type = engine_type
        if api_url is not None:
            self.api_url = api_url.strip()
        if api_key is not None:
            self.api_key = api_key.strip()
        if model is not None:
            self.model = model.strip()
        if gemini_api_key is not None:
            self.gemini_api_key = gemini_api_key.strip()
        if gemini_voice is not None:
            self.gemini_voice = gemini_voice.strip()
        if gemini_model is not None:
            self.gemini_model = gemini_model.strip()

    def toggle(self) -> bool:
        self.enabled = not self.enabled
        if not self.enabled:
            self.stop()
        return self.enabled

    def stop(self):
        """Immediately stop any currently playing speech."""
        with self._state_lock:
            self._cancel_generation += 1
            self._stop_playback()

    def _stop_playback(self):
        if IS_WIN and winmm:
            try:
                winmm.mciSendStringW(f"close {self._current_alias}", None, 0, None)
            except Exception:
                pass
        elif self._current_process:
            try:
                if self._current_process.poll() is None:
                    self._current_process.terminate()
            except Exception:
                pass
            self._current_process = None

    def speak_async(self, text: str):
        with self._state_lock:
            if not self.enabled or not text:
                return
            generation = self._cancel_generation
            clean_text = self._clean_text_for_speech(text)
        threading.Thread(target=self._run_tts_and_play, args=(clean_text, generation), daemon=True).start()

    def _clean_text_for_speech(self, text: str) -> str:
        # Filter markdown symbols and emotion tags like [confident], [shocked]
        import re
        t = re.sub(r"\[(confident|curious|shocked|relieved)\]", "", text, flags=re.IGNORECASE)
        t = t.replace("*", "").replace("#", "").replace("`", "").replace("~", "")
        return t.strip()

    def _run_tts_and_play(self, text: str, generation=None):
        if generation is None:
            generation = self._cancel_generation
        with self._lock:
            try:
                # Stop existing playback before starting new one
                with self._state_lock:
                    if not self.enabled or generation != self._cancel_generation:
                        return
                    self._stop_playback()

                # Unique filename every time to prevent file-locking
                unique_id = uuid.uuid4().hex[:10]
                audio_path = None

                # Mode 1: Gemini 3.8 Flash TTS
                if self.engine_type == "gemini_tts":
                    audio_path = self._generate_gemini_tts(text, unique_id)
                    if not audio_path:
                        print("[TTS] Gemini TTS generation failed, falling back to Edge-TTS", flush=True)
                        audio_path = os.path.join(self._temp_dir, f"speech_{unique_id}.mp3")
                        self._generate_edge_tts(text, audio_path)

                # Mode 2: OpenAI-compatible custom TTS endpoint (GPT-SoVITS / CosyVoice / Fish Audio)
                elif self.engine_type == "openai_tts" and self.api_url and requests:
                    mp3_path = os.path.join(self._temp_dir, f"speech_{unique_id}.mp3")
                    try:
                        headers = {"Content-Type": "application/json"}
                        if self.api_key:
                            headers["Authorization"] = f"Bearer {self.api_key}"
                        payload = {
                            "model": self.model or "tts-1",
                            "input": text,
                            "voice": self.voice or "default"
                        }
                        endpoint = self.api_url
                        if not endpoint.endswith("/v1/audio/speech") and not endpoint.endswith("/speech") and "/v1" not in endpoint:
                            endpoint = endpoint.rstrip("/") + "/v1/audio/speech"

                        resp = requests.post(endpoint, json=payload, headers=headers, timeout=12)
                        if resp.status_code == 200 and len(resp.content) > 100:
                            with open(mp3_path, "wb") as f:
                                f.write(resp.content)
                            audio_path = mp3_path
                        else:
                            print(f"[TTS] Custom TTS API failed ({resp.status_code}): {resp.text[:120]}, falling back to Edge-TTS", flush=True)
                            self._generate_edge_tts(text, mp3_path)
                            audio_path = mp3_path
                    except Exception as e:
                        print(f"[TTS] Custom TTS error: {e}, falling back to Edge-TTS", flush=True)
                        self._generate_edge_tts(text, mp3_path)
                        audio_path = mp3_path
                else:
                    # Mode 3: Edge-TTS
                    audio_path = os.path.join(self._temp_dir, f"speech_{unique_id}.mp3")
                    self._generate_edge_tts(text, audio_path)

                if audio_path and os.path.exists(audio_path):
                    with self._state_lock:
                        if self.enabled and generation == self._cancel_generation:
                            self._play_audio(audio_path)
                    self._cleanup_old_files()
            except Exception as e:
                print(f"[TTS] Error generating or playing speech: {e}", flush=True)

    def _generate_gemini_tts(self, text: str, unique_id: str) -> Optional[str]:
        if not requests:
            print("[TTS] requests library is not installed, cannot use Gemini TTS", flush=True)
            return None

        # Resolve API Key: passed key -> tts_api_key -> config_manager api_key
        key = self.gemini_api_key or self.api_key
        if not key:
            try:
                from config_manager import ConfigManager
                key = ConfigManager().get("api_key", "").strip()
            except Exception:
                pass

        if not key:
            print("[TTS] No Gemini API key found for Gemini TTS", flush=True)
            return None

        model_name = self.gemini_model or "gemini-3.8-flash-tts"
        voice_name = self.gemini_voice or "Aoede"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={key}"

        payload = {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {
                            "voiceName": voice_name
                        }
                    }
                }
            }
        }

        try:
            resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    for part in parts:
                        if "inlineData" in part:
                            inline = part["inlineData"]
                            mime = inline.get("mimeType", "audio/wav")
                            b64_data = inline.get("data", "")
                            if b64_data:
                                ext = ".wav" if "wav" in mime else ".mp3"
                                out_path = os.path.join(self._temp_dir, f"speech_{unique_id}{ext}")
                                with open(out_path, "wb") as f:
                                    f.write(base64.b64decode(b64_data))
                                return out_path
            print(f"[TTS] Gemini TTS API returned {resp.status_code}: {resp.text[:120]}", flush=True)
        except Exception as e:
            print(f"[TTS] Gemini TTS request error: {e}", flush=True)
        return None

    def _generate_edge_tts(self, text: str, output_path: str):
        voice = self.voice or "zh-CN-XiaoyiNeural"
        async def _generate():
            communicate = edge_tts.Communicate(
                text=text,
                voice=voice,
                rate=self.rate,
                pitch=self.pitch
            )
            await communicate.save(output_path)

        asyncio.run(_generate())

    def _play_audio(self, filepath: str):
        if IS_WIN and winmm:
            try:
                # Safely close existing MCI alias
                winmm.mciSendStringW(f"close {self._current_alias}", None, 0, None)
                time.sleep(0.05)
                # Open audio:
                file_ext = os.path.splitext(filepath)[1].lower()
                type_clause = "type mpegvideo " if file_ext == ".mp3" else ""
                cmd_open = f'open "{filepath}" {type_clause}alias {self._current_alias}'
                res_open = winmm.mciSendStringW(cmd_open, None, 0, None)
                if res_open != 0 and type_clause:
                    # Retry without type clause
                    cmd_open = f'open "{filepath}" alias {self._current_alias}'
                    res_open = winmm.mciSendStringW(cmd_open, None, 0, None)
                if res_open == 0:
                    winmm.mciSendStringW(f"play {self._current_alias}", None, 0, None)
                elif file_ext == ".wav":
                    import winsound
                    winsound.PlaySound(filepath, winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception as e:
                print(f"[TTS] Windows MCI Play Error: {e}", flush=True)
        elif IS_MAC:
            try:
                # macOS built-in native audio player supports both .wav and .mp3
                self._current_process = subprocess.Popen(
                    ["afplay", filepath],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            except Exception as e:
                print(f"[TTS] macOS afplay Error: {e}", flush=True)
        else:
            # Linux fallback (paplay / ffplay / aplay / mpv)
            for player in ["mpv", "ffplay", "paplay", "aplay"]:
                try:
                    args = [player, filepath]
                    if player == "ffplay":
                        args = [player, "-nodisp", "-autoexit", "-loglevel", "quiet", filepath]
                    elif player == "mpv":
                        args = [player, "--no-video", filepath]
                    self._current_process = subprocess.Popen(
                        args,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL
                    )
                    break
                except FileNotFoundError:
                    continue
                except Exception as e:
                    print(f"[TTS] Linux {player} Error: {e}", flush=True)

    def _play_mp3(self, filepath: str):
        self._play_audio(filepath)

    def _cleanup_old_files(self):
        try:
            files = glob.glob(os.path.join(self._temp_dir, "speech_*.*"))
            # Keep only the last 5 audio files
            if len(files) > 5:
                files.sort(key=os.path.getmtime)
                for f in files[:-5]:
                    try:
                        os.remove(f)
                    except Exception:
                        pass
        except Exception:
            pass
