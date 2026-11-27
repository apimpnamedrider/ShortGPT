import asyncio
import hmac
import json
import os
import re
import shutil
import subprocess
import time
import threading
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


async def _make_voice(text, voice, audio_path):
    communicate = edge_tts.Communicate(text=text, voice=voice, rate="+0%")
    await communicate.save(str(audio_path))


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
    """Render real 3D miniature mechanisms and camera animation with Blender."""
    try:
        storyboard = json.loads(storyboard_json) if storyboard_json else {}
    except (TypeError, ValueError):
        raise ValueError("The scene storyboard is not valid JSON.")
    scenes = storyboard.get("scenes") if isinstance(storyboard, dict) else None
    if not isinstance(scenes, list) or not 3 <= len(scenes) <= 10:
        raise ValueError("Generate a storyboard with 3 to 10 scenes before rendering.")

    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = VIDEO_DIR / f"shorts-forge-{uuid.uuid4().hex}.mp4"
    silent_path = work_dir / "fern-scenes.mp4"
    storyboard_path = work_dir / "storyboard.json"
    audio_path = work_dir / "narration.mp3"

    clean_scenes = []
    cursor = 0.0
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            continue
        start, end = _parse_scene_time(
            scene.get("time"), cursor, 60.0 * (index + 1) / len(scenes)
        )
        start = max(cursor, start)
        end = max(start + 2.0, min(60.0, end))
        voice_line = str(scene.get("voice") or "").strip()
        visual_line = str(scene.get("visual") or "").strip()
        components = scene.get("components") if isinstance(scene.get("components"), list) else []
        components = [str(x).strip()[:34] for x in components[:4] if str(x).strip()]
        if len(components) < 3:
            raise ValueError(
                f"Scene {index + 1} needs three named physical components for 3D construction."
            )
        if not voice_line or not visual_line:
            raise ValueError(f"Scene {index + 1} is missing narration or visual direction.")
        clean_scenes.append({
            "time": f"{start:.2f}-{end:.2f}s",
            "voice": voice_line,
            "visual": visual_line,
            "components": components,
            "camera": str(scene.get("camera") or "slow push-in"),
            "sfx": str(scene.get("sfx") or "").strip(),
        })
        cursor = end

    if len(clean_scenes) < 3:
        raise ValueError("The storyboard does not contain enough complete 3D scenes.")
    clean_scenes[0]["time"] = "0.00-" + clean_scenes[0]["time"].split("-", 1)[1]
    for index in range(len(clean_scenes) - 1):
        next_start = float(clean_scenes[index + 1]["time"].split("-", 1)[0])
        clean_scenes[index]["time"] = clean_scenes[index]["time"].split("-", 1)[0] + f"-{next_start:.2f}s"
    clean_scenes[-1]["time"] = clean_scenes[-1]["time"].split("-", 1)[0] + "-60.00s"

    narration = " ".join(scene["voice"] for scene in clean_scenes)
    voice = "en-ZA-LukeNeural"
    try:
        selected_language = Language(language)
        voice = EDGE_TTS_VOICENAME_MAPPING[selected_language]["male"]
    except Exception:
        pass
    asyncio.run(_make_voice(narration, voice, audio_path))
    if not audio_path.is_file() or audio_path.stat().st_size < 1000:
        raise RuntimeError("Narration audio could not be generated; refusing to create a silent video.")

    storyboard_path.write_text(
        json.dumps({"topic": topic, "scenes": clean_scenes}, ensure_ascii=False),
        encoding="utf-8",
    )
    blender = shutil.which("blender")
    if not blender:
        raise RuntimeError("The Blender 3D engine is not installed on this worker.")
    blender_script = Path(__file__).with_name("fern_blender_render.py")
    if not blender_script.is_file():
        raise RuntimeError("The Fern 3D scene builder is missing from the worker deployment.")

    print(f"Starting Blender 3D render: {len(clean_scenes)} scenes, 12 fps, 9:16.")
    _run([
        blender, "-b", "--python", str(blender_script), "--",
        str(storyboard_path), str(silent_path),
    ], timeout=3600)
    if not silent_path.is_file() or silent_path.stat().st_size < 10000:
        raise RuntimeError("Blender completed without producing its 3D scene video.")

    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(silent_path), "-i", str(audio_path),
        "-vf", "fps=24,scale=1080:1920:flags=lanczos,format=yuv420p",
        "-t", "60", "-map", "0:v:0", "-map", "1:a:0",
        "-af", "apad", "-c:v", "libx264", "-preset", "veryfast",
        "-threads", "2", "-crf", "19", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", str(output_path),
    ], timeout=900)
    _validate_mp4(output_path)
    return output_path


_RENDER_JOBS = {}
_RENDER_JOBS_LOCK = threading.Lock()


def start_render_job(topic, language, token, storyboard_json):
    """Queue a render on a daemon thread and return immediately."""
    job_id = uuid.uuid4().hex
    with _RENDER_JOBS_LOCK:
        _RENDER_JOBS[job_id] = {
            "status": "queued", "progress": 0, "videoUrl": None, "error": None,
            "createdAt": time.time()
        }

    def run():
        with _RENDER_JOBS_LOCK:
            _RENDER_JOBS[job_id]["status"] = "rendering"
        try:
            video_url = render_video(topic, language, token, storyboard_json)
            with _RENDER_JOBS_LOCK:
                _RENDER_JOBS[job_id].update({
                    "status": "completed", "progress": 100, "videoUrl": video_url
                })
        except Exception as exc:
            print(f"Render job {job_id} failed: {type(exc).__name__}: {exc}")
            with _RENDER_JOBS_LOCK:
                _RENDER_JOBS[job_id].update({
                    "status": "failed", "error": str(exc)[:1000]
                })

    thread = threading.Thread(target=run, name=f"render-{job_id[:8]}", daemon=True)
    thread.start()
    return job_id


def get_render_job(job_id):
    with _RENDER_JOBS_LOCK:
        job = _RENDER_JOBS.get(job_id)
        return dict(job) if job else None


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
