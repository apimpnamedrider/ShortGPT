import asyncio
import hmac
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import edge_tts

from shortGPT.audio.edge_voice_module import EdgeTTSVoiceModule
from shortGPT.config.api_db import ApiKeyManager
from shortGPT.config.languages import EDGE_TTS_VOICENAME_MAPPING, Language
from shortGPT.engine.content_video_engine import ContentVideoEngine
from shortGPT.gpt import gpt_chat_video


VIDEO_DIR = Path(os.environ.get("VIDEO_OUTPUT_DIR", "/app/videos"))
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
]


def _run(command, timeout=900):
    completed = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    return completed.stdout.strip()


def _validate_mp4(path):
    if not path.is_file() or path.stat().st_size < 10_000:
        raise RuntimeError("The renderer did not create a usable MP4 file.")
    probe = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height",
        "-of", "default=noprint_wrappers=1", str(path),
    ], timeout=30)
    if "codec_name=" not in probe or "width=1080" not in probe or "height=1920" not in probe:
        raise RuntimeError("The rendered file is not a valid 1080x1920 vertical MP4.")
    duration = float(_run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ], timeout=30))
    if duration < 5:
        raise RuntimeError("The rendered MP4 is too short to be a usable video.")
    return {"duration": round(duration, 2), "bytes": path.stat().st_size, "probe": probe}


def _cleanup_old_outputs():
    now = time.time()
    for item in VIDEO_DIR.iterdir():
        try:
            age = now - item.stat().st_mtime
            if item.is_dir() and item.name.startswith("job-") and age > 24 * 60 * 60:
                shutil.rmtree(item, ignore_errors=True)
            elif item.is_file() and item.suffix.lower() == ".mp4" and age > 30 * 24 * 60 * 60:
                item.unlink(missing_ok=True)
        except OSError as exc:
            print(f"Output cleanup skipped for {item.name}: {exc}")


def _fallback_script(topic):
    clean_topic = re.sub(r"\\s+", " ", topic).strip().rstrip(".?!")
    return (
        f"Here is how {clean_topic} works. "
        "Start with the basic idea: every part has a job, and each step causes the next one. "
        "First, the system takes in what it needs. Then its key components transform that input "
        "into something useful. The important detail is the process happening inside, often too "
        "small or too fast to notice. As the parts work together, energy or information moves "
        "through the system. The final stage turns that process into a result you can see and use. "
        f"So the next time you encounter {clean_topic}, remember: the real story is the sequence "
        "of small steps working together."
    )


async def _make_voice(text, voice, audio_path):
    communicate = edge_tts.Communicate(text=text, voice=voice, rate="+0%")
    await communicate.save(str(audio_path))


def _fallback_render(topic, language, work_dir):
    """Low-memory FFmpeg fallback; a static title card with optional narration."""
    work_dir.mkdir(parents=True, exist_ok=True)
    safe_topic = re.sub(r"\\s+", " ", topic).strip()
    narration = _fallback_script(safe_topic)
    title_path = work_dir / "title.txt"
    title_path.write_text(safe_topic[:90], encoding="utf-8")
    audio_path = work_dir / "narration.mp3"
    output_path = VIDEO_DIR / f"shorts-forge-{uuid.uuid4().hex}.mp4"

    voice = "en-ZA-LukeNeural"
    try:
        selected_language = Language(language)
        voice = EDGE_TTS_VOICENAME_MAPPING[selected_language]["male"]
    except Exception:
        pass

    has_audio = False
    try:
        asyncio.run(_make_voice(narration, voice, audio_path))
        has_audio = audio_path.is_file() and audio_path.stat().st_size > 1000
    except Exception as exc:
        print(f"Fallback voice generation unavailable; creating silent MP4: {exc}")

    font = next((item for item in FONT_CANDIDATES if Path(item).is_file()), None)
    if not font:
        raise RuntimeError("No usable font is installed for FFmpeg text rendering.")

    draw = (
        f"drawtext=fontfile='{font}':textfile='{title_path}':"
        "fontcolor=white:fontsize=64:line_spacing=18:"
        "x=(w-text_w)/2:y=(h-text_h)/2:"
        "box=1:boxcolor=0x111827@0.72:boxborderw=42"
    )
    # Generate one static source frame per second and encode with one thread.
    # FFmpeg repeats it to 24 fps; this avoids a 30-fps raw 1080p frame queue.
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=0x080D18:s=1080x1920:r=1:d=60",
    ]
    if has_audio:
        command += ["-i", str(audio_path)]
    command += [
        "-vf", draw, "-t", "60", "-r", "24",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
        "-threads", "1", "-crf", "30", "-pix_fmt", "yuv420p",
    ]
    if has_audio:
        command += [
            "-map", "0:v:0", "-map", "1:a:0", "-af", "apad",
            "-t", "60", "-c:a", "aac", "-b:a", "96k",
        ]
    else:
        command += ["-an"]
    command += ["-movflags", "+faststart", str(output_path)]
    _run(command, timeout=900)
    _validate_mp4(output_path)
    return output_path


def render_video(topic: str, language: str, token: str) -> str:
    """Render a vertical MP4 and return a Gradio-downloadable URL."""
    expected_token = os.environ.get("SHORTGPT_API_TOKEN", "")
    if not expected_token or not hmac.compare_digest(str(token or ""), expected_token):
        raise ValueError("Invalid worker token.")

    topic = (topic or "").strip()
    if not topic or len(topic) > 600:
        raise ValueError("Enter a topic between 1 and 600 characters.")

    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_old_outputs()
    work_dir = VIDEO_DIR / f"job-{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = None

    has_ai_key = bool(
        ApiKeyManager.get_api_key("OPENAI_API_KEY")
        or ApiKeyManager.get_api_key("GEMINI_API_KEY")
    )
    has_pexels_key = bool(ApiKeyManager.get_api_key("PEXELS_API_KEY"))

    if has_ai_key and has_pexels_key:
        try:
            selected_language = Language(language)
            voice_module = EdgeTTSVoiceModule(
                EDGE_TTS_VOICENAME_MAPPING[selected_language]["male"]
            )
            script = gpt_chat_video.generateScript(topic, selected_language.value)
            if not script:
                raise RuntimeError("ShortGPT could not generate a script.")
            engine = ContentVideoEngine(
                voiceModule=voice_module,
                script=script,
                isVerticalFormat=True,
                language=selected_language,
            )
            for _step, _description in engine.makeContent():
                pass
            candidate = engine.get_video_output_path()
            if not candidate or not os.path.isfile(candidate):
                raise RuntimeError("ShortGPT did not produce an MP4 file.")
            output_path = Path(candidate)
            _validate_mp4(output_path)
        except Exception as exc:
            print(f"Primary ShortGPT render failed; switching to FFmpeg fallback: {exc}")

    if output_path is None:
        output_path = _fallback_render(topic, language, work_dir)

    _validate_mp4(output_path)
    public_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if not public_domain:
        raise RuntimeError("Railway public domain is not configured.")
    return "https://" + public_domain + "/gradio_api/file=" + quote(str(output_path), safe="/")
