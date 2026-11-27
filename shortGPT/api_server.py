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
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "No FFmpeg diagnostic output").strip()
        print(f"Render command failed (exit {completed.returncode}): {detail[-6000:]}")
        raise RuntimeError(f"Render command failed: {detail[-1200:]}")
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
        # Persistent framing and animated camera-like scan line.
        "drawbox=x=72:y=170:w=936:h=2:color=0x334155@0.9:t=fill",
        "drawbox=x='72+mod(t*210,936)':y=170:w=120:h=4:color=0x38BDF8@0.95:t=fill",
        f"drawtext=fontfile='{font}':text='HOW IT WORKS':fontcolor=0x38BDF8:fontsize=30:x=82:y=95",
        f"drawtext=fontfile='{font}':textfile='{title_path}':fontcolor=white:fontsize=48:x=82:y=220:box=1:boxcolor=0x101C2F@0.85:boxborderw=18",
        # Scene 1 — separate input pieces travel into an assembly area.
        "drawbox=x=90:y=650:w=900:h=560:color=0x101C2F@0.88:t=fill:enable='between(t,0,19.99)'",
        "drawbox=x=90:y=650:w=900:h=3:color=0x38BDF8@0.9:t=fill:enable='between(t,0,19.99)'",
        "drawbox=x='120+mod(t*115,650)':y=780:w=92:h=92:color=0x38BDF8@0.95:t=fill:enable='between(t,0,19.99)'",
        "drawbox=x='820-mod(t*105,650)':y=980:w=92:h=92:color=0xA78BFA@0.95:t=fill:enable='between(t,0,19.99)'",
        "drawbox=x=485:y=855:w=110:h=110:color=0xE2E8F0@0.95:t=fill:enable='between(t,0,19.99)'",
        "drawbox=x='485+30*sin(t*2)':y=965:w=110:h=5:color=0x38BDF8@0.95:t=fill:enable='between(t,0,19.99)'",
        f"drawtext=fontfile='{font}':text='01  INPUT  /  COMPONENTS COME TOGETHER':fontcolor=white:fontsize=27:x=130:y=1110:enable='between(t,0,19.99)'",
        # Scene 2 — an exploded, layered mechanism with parts moving around a core.
        "drawbox=x=90:y=650:w=900:h=560:color=0x101C2F@0.88:t=fill:enable='between(t,20,39.99)'",
        "drawbox=x=90:y=650:w=900:h=3:color=0xA78BFA@0.95:t=fill:enable='between(t,20,39.99)'",
        "drawbox=x='170+24*sin(t*2)':y=760:w=150:h=150:color=0x38BDF8@0.95:t=fill:enable='between(t,20,39.99)'",
        "drawbox=x=465:y='750+24*sin(t*2+1)':w=150:h=150:color=0xA78BFA@0.95:t=fill:enable='between(t,20,39.99)'",
        "drawbox=x='760-24*sin(t*2+2)':y=940:w=150:h=150:color=0xF59E0B@0.95:t=fill:enable='between(t,20,39.99)'",
        "drawbox=x='320+145*sin(t*1.7)':y='900+90*cos(t*1.7)':w=72:h=72:color=0xE2E8F0@0.95:t=fill:enable='between(t,20,39.99)'",
        "drawbox=x=485:y=920:w=110:h=5:color=0xA78BFA@0.95:t=fill:enable='between(t,20,39.99)'",
        f"drawtext=fontfile='{font}':text='02  PROCESS  /  THE HIDDEN MECHANISM':fontcolor=white:fontsize=27:x=130:y=1110:enable='between(t,20,39.99)'",
        # Scene 3 — assembled result travels into view and pulses.
        "drawbox=x=90:y=650:w=900:h=560:color=0x101C2F@0.88:t=fill:enable='between(t,40,60)'",
        "drawbox=x=90:y=650:w=900:h=3:color=0x34D399@0.95:t=fill:enable='between(t,40,60)'",
        "drawbox=x='150+35*sin(t*1.2)':y=800:w=190:h=190:color=0x38BDF8@0.95:t=fill:enable='between(t,40,60)'",
        "drawbox=x=445:y='790+25*sin(t*1.6)':w=190:h=190:color=0x34D399@0.95:t=fill:enable='between(t,40,60)'",
        "drawbox=x='740-35*sin(t*1.2)':y=800:w=190:h=190:color=0xA78BFA@0.95:t=fill:enable='between(t,40,60)'",
        f"drawtext=fontfile='{font}':text='03  RESULT  /  THE COMPLETE SYSTEM':fontcolor=white:fontsize=27:x=130:y=1110:enable='between(t,40,60)'",
        # Moving particles and timeline make motion visible throughout the short.
        "drawbox=x='mod(t*190,900)+90':y=1320:w=34:h=8:color=0x38BDF8@0.95:t=fill",
        "drawbox=x=90:y=1370:w='min(900,t*15)':h=8:color=0x38BDF8@0.95:t=fill",
        f"drawtext=fontfile='{font}':text='FOLLOW THE PROCESS':fontcolor=0xCBD5E1:fontsize=26:x=(w-text_w)/2:y=1460",
        f"drawtext=fontfile='{font}':text='SHORTS FORGE  •  60 SECOND EXPLAINER':fontcolor=0x94A3B8:fontsize=21:x=(w-text_w)/2:y=1790",
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
