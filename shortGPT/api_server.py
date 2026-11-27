import asyncio
import hmac
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import quote

import edge_tts

from shortGPT.config.languages import EDGE_TTS_VOICENAME_MAPPING, Language


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



def _parse_scene_time(value, fallback_start, fallback_end):
    """Accept ranges such as 0-12s, 12–24s, or a plain duration."""
    raw = str(value or "")
    nums = re.findall(r"\d+(?:\.\d+)?", raw)
    if len(nums) >= 2:
        start, end = float(nums[0]), float(nums[1])
    elif len(nums) == 1:
        start, end = fallback_start, float(nums[0])
    else:
        start, end = fallback_start, fallback_end
    start = max(0.0, min(59.0, start))
    end = max(start + 1.0, min(60.0, end))
    return start, end


def _scene_driven_render(topic, language, work_dir, storyboard_json):
    """Render each authored storyboard scene as its own timed animated motion-graphics sequence."""
    try:
        storyboard = json.loads(storyboard_json) if storyboard_json else {}
    except (TypeError, ValueError):
        raise ValueError("The scene storyboard is not valid JSON.")
    scenes = storyboard.get("scenes") if isinstance(storyboard, dict) else None
    if not isinstance(scenes, list) or not 3 <= len(scenes) <= 8:
        raise ValueError("Generate a storyboard with 3 to 8 scenes before rendering.")

    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = VIDEO_DIR / f"shorts-forge-{uuid.uuid4().hex}.mp4"
    font = next((item for item in FONT_CANDIDATES if Path(item).is_file()), None)
    if not font:
        raise RuntimeError("No usable font is installed for FFmpeg text rendering.")

    clean_scenes = []
    cursor = 0.0
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            continue
        start, end = _parse_scene_time(scene.get("time"), cursor, 60.0 * (index + 1) / len(scenes))
        if start < cursor:
            start = cursor
        if end <= start:
            end = min(60.0, start + 1.0)
        voice_line = str(scene.get("voice") or "").strip()
        visual_line = str(scene.get("visual") or "").strip()
        if not voice_line or not visual_line:
            raise ValueError(f"Scene {index + 1} is missing its narration or visual direction.")
        components = scene.get("components") if isinstance(scene.get("components"), list) else []
        components = [str(x).strip()[:42] for x in components[:3] if str(x).strip()]
        if not components:
            components = [visual_line[:42], "HIDDEN MECHANISM", "OUTPUT"]
        clean_scenes.append({
            "start": start, "end": end, "voice": voice_line,
            "visual": visual_line, "components": components,
            "camera": str(scene.get("camera") or "slow push-in").lower(),
            "sfx": str(scene.get("sfx") or "").strip(),
        })
        cursor = end

    if len(clean_scenes) < 3:
        raise ValueError("The storyboard does not contain enough complete scenes.")
    # Close timing gaps and clamp the complete short to exactly 60 seconds.
    clean_scenes[0]["start"] = 0.0
    for index in range(len(clean_scenes) - 1):
        clean_scenes[index]["end"] = clean_scenes[index + 1]["start"]
    clean_scenes[-1]["end"] = 60.0
    if any(s["end"] - s["start"] < 2.0 for s in clean_scenes):
        raise ValueError("Each storyboard scene needs at least two seconds.")

    narration = " ".join(scene["voice"] for scene in clean_scenes)
    audio_path = work_dir / "narration.mp3"
    voice = "en-ZA-LukeNeural"
    try:
        selected_language = Language(language)
        voice = EDGE_TTS_VOICENAME_MAPPING[selected_language]["male"]
    except Exception:
        pass
    asyncio.run(_make_voice(narration, voice, audio_path))
    if not audio_path.is_file() or audio_path.stat().st_size < 1000:
        raise RuntimeError("Narration audio could not be generated; refusing to create a silent video.")

    # Each scene gets its own visual copy, labels, accent colour and motion interval.
    # The scene-specific elements move continuously; this is not a still-image slideshow.
    accents = ["0x39D5C8", "0xA78BFA", "0xF5B942", "0x60A5FA", "0xFB7185", "0x34D399"]
    filters = [
        "drawbox=x=0:y=0:w=1080:h=1920:color=0x070B13@1:t=fill",
        "drawbox=x=72:y=160:w=936:h=2:color=0x263449@1:t=fill",
        "drawbox=x='72+mod(t*180,936)':y=158:w=130:h=5:color=0x39D5C8@0.95:t=fill",
        f"drawtext=fontfile='{font}':text='HOW IT WORKS':fontcolor=0x39D5C8:fontsize=28:x=82:y=95",
        "drawbox=x=72:y=1690:w=936:h=3:color=0x263449@1:t=fill",
        "drawbox=x=72:y=1690:w='936*t/60':h=4:color=0x39D5C8@0.95:t=fill",
    ]
    for index, scene in enumerate(clean_scenes):
        start, end = scene["start"], scene["end"]
        accent = accents[index % len(accents)]
        safe_start = f"{start:.3f}"
        safe_end = f"{end - 0.02:.3f}"
        title_file = work_dir / f"scene-{index + 1}-title.txt"
        visual_file = work_dir / f"scene-{index + 1}-visual.txt"
        component_files = []
        title_file.write_text(f"{index + 1:02d}  {scene['components'][0]}", encoding="utf-8")
        visual_file.write_text(scene["visual"][:180], encoding="utf-8")
        for component_index, component in enumerate(scene["components"]):
            path = work_dir / f"scene-{index + 1}-component-{component_index + 1}.txt"
            path.write_text(component, encoding="utf-8")
            component_files.append(path)
        enable = f"between(t,{safe_start},{safe_end})"
        filters.extend([
            f"drawbox=x=70:y=520:w=940:h=930:color=0x101A2A@0.96:t=fill:enable='{enable}'",
            f"drawbox=x=70:y=520:w=940:h=4:color={accent}@0.95:t=fill:enable='{enable}'",
            f"drawtext=fontfile='{font}':text='SCENE {index + 1:02d} / {len(clean_scenes):02d}':fontcolor={accent}:fontsize=25:x=112:y=575:enable='{enable}'",
            f"drawtext=fontfile='{font}':textfile='{title_file}':fontcolor=white:fontsize=35:x=112:y=640:enable='{enable}'",
            f"drawtext=fontfile='{font}':textfile='{visual_file}':fontcolor=0xCBD5E1:fontsize=22:x=112:y=1325:line_spacing=12:enable='{enable}'",
            # Three independently moving, labelled mechanism layers.
            f"drawbox=x='190+55*sin(t*1.25)':y='800+18*sin(t*1.8)':w=205:h=150:color=0x172B40@1:t=fill:enable='{enable}'",
            f"drawbox=x='438+28*sin(t*1.1+1)':y='930+22*cos(t*1.5)':w=205:h=150:color=0x1D3047@1:t=fill:enable='{enable}'",
            f"drawbox=x='685-48*sin(t*1.2+2)':y='800+20*sin(t*1.7+2)':w=205:h=150:color=0x172B40@1:t=fill:enable='{enable}'",
            f"drawbox=x='395+110*sin(t*1.4)':y='1085+35*cos(t*1.6)':w=290:h=5:color={accent}@0.95:t=fill:enable='{enable}'",
            f"drawbox=x='220+mod(t*155,600)':y=1170:w=34:h=8:color={accent}@0.95:t=fill:enable='{enable}'",
        ])
        for component_index, path in enumerate(component_files):
            x = [205, 453, 700][component_index]
            y = [840, 970, 840][component_index]
            filters.append(
                f"drawtext=fontfile='{font}':textfile='{path}':fontcolor=white:fontsize=19:x={x}:y={y}:enable='{enable}'"
            )
        # Distinct camera rhythm per scene, represented by a moving scan/energy sweep.
        if "orbit" in scene["camera"] or "rotate" in scene["camera"]:
            filters.append(f"drawbox=x='120+mod(t*230,840)':y=1490:w=110:h=3:color={accent}@0.9:t=fill:enable='{enable}'")
        elif "pull" in scene["camera"] or "wide" in scene["camera"]:
            filters.append(f"drawbox=x='500-190*sin(t*0.8)':y=1490:w=110:h=3:color={accent}@0.9:t=fill:enable='{enable}'")
        else:
            filters.append(f"drawbox=x='120+85*sin(t*0.7)':y=1490:w=110:h=3:color={accent}@0.9:t=fill:enable='{enable}'")

    filters.append(f"drawtext=fontfile='{font}':text='SHORTS FORGE  /  VISUAL EXPLAINER':fontcolor=0x94A3B8:fontsize=20:x=(w-text_w)/2:y=1800")
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=c=0x070B13:s=1080x1920:r=30:d=60",
        "-i", str(audio_path), "-vf", ",".join(filters), "-t", "60",
        "-c:v", "libx264", "-preset", "veryfast", "-threads", "2",
        "-crf", "20", "-pix_fmt", "yuv420p", "-map", "0:v:0",
        "-map", "1:a:0", "-af", "apad", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", str(output_path),
    ]
    _run(command, timeout=900)
    _validate_mp4(output_path)
    return output_path


def render_video(topic: str, language: str, token: str, storyboard_json: str = "") -> str:
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

    if not storyboard_json:
        raise ValueError("A scene-by-scene storyboard is required. Generate a script before rendering.")
    output_path = _scene_driven_render(topic, language, work_dir, storyboard_json)

    _validate_mp4(output_path)
    public_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if not public_domain:
        raise RuntimeError("Railway public domain is not configured.")
    return "https://" + public_domain + "/gradio_api/file=" + quote(str(output_path), safe="/")
