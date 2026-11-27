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
    """Render a lightweight animated 9:16 explainer when stock footage is unavailable."""
    work_dir.mkdir(parents=True, exist_ok=True)
    safe_topic = re.sub(r"\\s+", " ", topic).strip()
    narration = _fallback_script(safe_topic)
    title_path = work_dir / "title.txt"
    title_path.write_text(safe_topic[:70], encoding="utf-8")
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

    # All motion is generated directly by FFmpeg expressions. This avoids loading
    # full-resolution frames into Python/RAM and produces genuine 24-fps animation.
    filters = [
        "drawbox=x='(W-260)*(0.5+0.5*sin(t*0.55))':y=250:w=260:h=5:color=0x38BDF8@0.8:t=fill",
        "drawbox=x=90:y='420+110*sin(t*0.7)':w=18:h=18:color=0x38BDF8@0.9:t=fill",
        "drawbox=x=972:y='520+130*cos(t*0.65)':w=18:h=18:color=0xA78BFA@0.9:t=fill",
        "drawbox=x='(W/2)+300*sin(t*0.8)-14':y='960+300*cos(t*0.8)-14':w=28:h=28:color=0x38BDF8@0.95:t=fill",
        "drawbox=x='(W/2)+300*sin(t*0.8+3.14)-14':y='960+300*cos(t*0.8+3.14)-14':w=28:h=28:color=0xA78BFA@0.95:t=fill",
        "drawbox=x=110:y=690:w=860:h=540:color=0x101C2F@0.92:t=fill",
        "drawbox=x=110:y=690:w=860:h=3:color=0x38BDF8@0.9:t=fill",
        f"drawtext=fontfile='{font}':text='HOW IT WORKS':fontcolor=0x38BDF8:fontsize=34:x=(w-text_w)/2:y=760",
        f"drawtext=fontfile='{font}':textfile='{title_path}':fontcolor=white:fontsize=58:line_spacing=16:x='(w-text_w)/2+18*sin(t*0.8)':y='890+12*sin(t*1.1)':box=1:boxcolor=0x080D18@0.35:boxborderw=24",
        f"drawtext=fontfile='{font}':text='FOLLOW THE PROCESS':fontcolor=0xCBD5E1:fontsize=27:x=(w-text_w)/2:y=1110",
        "drawbox=x=120:y=1370:w='min(840, t*14)':h=9:color=0x38BDF8@0.95:t=fill",
        f"drawtext=fontfile='{font}':text='01  INPUT':fontcolor=white:fontsize=42:x=(w-text_w)/2:y=1450:enable='between(t,0,19.99)'",
        f"drawtext=fontfile='{font}':text='02  PROCESS':fontcolor=white:fontsize=42:x=(w-text_w)/2:y=1450:enable='between(t,20,39.99)'",
        f"drawtext=fontfile='{font}':text='03  RESULT':fontcolor=white:fontsize=42:x=(w-text_w)/2:y=1450:enable='between(t,40,60)'",
        f"drawtext=fontfile='{font}':text='SHORTS FORGE  •  60 SECOND EXPLAINER':fontcolor=0x94A3B8:fontsize=22:x=(w-text_w)/2:y=1770",
    ]
    video_filter = ",".join(filters)
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=0x080D18:s=1080x1920:r=24:d=60",
    ]
    if has_audio:
        command += ["-i", str(audio_path)]
    command += [
        "-vf", video_filter, "-t", "60",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-threads", "1", "-crf", "27", "-pix_fmt", "yuv420p",
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
