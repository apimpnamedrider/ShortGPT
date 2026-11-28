import asyncio
import hmac
import json
import os
import re
import shutil
import subprocess
import time
import threading
import selectors
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
    """Run a media command while streaming output and render progress into job state."""
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, errors="replace"
    )
    output = []
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while True:
            if time.monotonic() >= deadline:
                process.kill()
                process.wait()
                raise RuntimeError(f"Render command timed out after {timeout} seconds.")
            events = selector.select(timeout=1.0)
            if events:
                line = process.stdout.readline()
                if not line and process.poll() is not None:
                    break
                if line:
                    output.append(line)
                    if len(output) > 3000:
                        output = output[-2000:]
                    match = re.search(r"(?:MECHCGI|FERN)_PROGRESS\s+(\d+)\s+(\d+)", line)
                    job_id = getattr(_JOB_CONTEXT, "job_id", None)
                    if match and job_id:
                        current, total = int(match.group(1)), max(1, int(match.group(2)))
                        with _RENDER_JOBS_LOCK:
                            job = _RENDER_JOBS.get(job_id)
                            if job:
                                job["progress"] = min(85, 10 + int(75 * current / total))
                                job["stage"] = f"Rendering 3D frames ({current}/{total})"
                                job["updatedAt"] = time.time()
                    elif line.strip():
                        print(line.rstrip()[-1200:], flush=True)
            elif process.poll() is not None:
                # Drain any final buffered output before exiting.
                remainder = process.stdout.read()
                if remainder:
                    output.append(remainder)
                break
        return_code = process.wait()
    finally:
        selector.close()
        if process.stdout:
            process.stdout.close()
    combined = "".join(output)
    if return_code != 0:
        detail = combined.strip() or "No media diagnostic output"
        print(f"Render command failed (exit {return_code}): {detail[-6000:]}", flush=True)
        raise RuntimeError(f"Render command failed: {detail[-1200:]}")
    return combined.strip()


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


def _voice_candidates(language, preferred_voice):
    candidates = [preferred_voice]
    try:
        selected = Language(language)
        mapping = EDGE_TTS_VOICENAME_MAPPING.get(selected, {})
        candidates.extend([mapping.get("female"), mapping.get("male")])
    except Exception:
        pass
    # Use separate regional voices as fallbacks for the common English selection.
    if str(language).lower() == "english":
        candidates.extend([
            "en-ZA-LukeNeural",
            "en-ZA-LeahNeural",
            "en-AU-WilliamNeural",
            "en-US-GuyNeural",
            "en-GB-RyanNeural",
        ])
    unique = []
    for item in candidates:
        if item and item not in unique:
            unique.append(item)
    return unique


def _local_voice_for(language):
    code = {
        "English": "en-za",
        "Afrikaans": "af",
        "Zulu": "zu",
    }.get(str(language), "en")
    try:
        available = subprocess.run(
            ["espeak-ng", "--voices"], capture_output=True, text=True,
            timeout=10, check=True
        ).stdout
        matches = []
        for line in available.splitlines():
            fields = line.split()
            if len(fields) >= 4 and fields[1].lower().startswith(code):
                matches.append(fields[3])
        if matches:
            return matches[0]
    except Exception:
        pass
    if code == "en-za":
        return "en"
    return None


async def _make_voice(text, voice, audio_path, language="English"):
    """Generate speech with retrying cloud voices and a local offline safety net."""
    failures = []
    for attempt, candidate in enumerate(_voice_candidates(language, voice), start=1):
        try:
            audio_path.unlink(missing_ok=True)
            communicate = edge_tts.Communicate(text=text, voice=candidate, rate="+0%")
            await asyncio.wait_for(communicate.save(str(audio_path)), timeout=90)
            if audio_path.is_file() and audio_path.stat().st_size >= 1000:
                print(f"Narration generated with Edge TTS voice {candidate}.", flush=True)
                return
            raise RuntimeError("The selected voice returned an empty audio file.")
        except Exception as exc:
            failures.append(f"{candidate}: {type(exc).__name__}: {exc}")
            audio_path.unlink(missing_ok=True)
            print(f"Edge TTS attempt {attempt} failed for {candidate}: {type(exc).__name__}", flush=True)
            await asyncio.sleep(min(2 * attempt, 6))

    local_voice = _local_voice_for(language)
    if local_voice:
        wav_path = audio_path.with_suffix(".wav")
        try:
            wav_path.unlink(missing_ok=True)
            subprocess.run(
                ["espeak-ng", "-v", local_voice, "-s", "150", "-w", str(wav_path), text],
                capture_output=True, text=True, timeout=120, check=True
            )
            if not wav_path.is_file() or wav_path.stat().st_size < 1000:
                raise RuntimeError("Offline speech engine produced an empty WAV.")
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i",
                 str(wav_path), "-codec:a", "libmp3lame", "-q:a", "4", str(audio_path)],
                capture_output=True, text=True, timeout=120, check=True
            )
            wav_path.unlink(missing_ok=True)
            if audio_path.is_file() and audio_path.stat().st_size >= 1000:
                print(f"Narration generated with offline espeak-ng voice {local_voice}.", flush=True)
                return
        except Exception as exc:
            wav_path.unlink(missing_ok=True)
            failures.append(f"offline {local_voice}: {type(exc).__name__}: {exc}")
    raise RuntimeError(
        "Narration generation failed after cloud retries and offline fallback. "
        + " | ".join(failures[-6:])[-1200:]
    )


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
    silent_path = work_dir / "mechanical-cgi-scenes.mp4"
    storyboard_path = work_dir / "storyboard.json"
    audio_path = work_dir / "narration.mp3"

    clean_scenes = []
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            continue
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
        words = len(re.findall(r"\b[\w’'-]+\b", voice_line))
        if words > 22:
            raise ValueError(
                f"Scene {index + 1} contains {words} spoken words. Split it into shorter narration-matched shots."
            )
        illustration_line = str(scene.get("illustration") or "").strip()
        animation_line = str(scene.get("animation") or "").strip()
        camera_detail_line = str(scene.get("cameraDetail") or "").strip()
        if not illustration_line or not animation_line or not camera_detail_line:
            raise ValueError(
                f"Scene {index + 1} needs a detailed illustration, subject animation, and camera plan."
            )
        clean_scenes.append({
            "voice": voice_line,
            "visual": visual_line,
            "illustration": illustration_line[:900],
            "animation": animation_line[:700],
            "cameraDetail": camera_detail_line[:500],
            "components": components,
            "camera": str(scene.get("camera") or "macro push-in"),
            "sfx": str(scene.get("sfx") or "").strip(),
            "_words": max(1, words),
        })

    if not 8 <= len(clean_scenes) <= 10:
        raise ValueError("Narration-led videos must contain 8 to 10 distinct micro-shots.")
    # Edge TTS speaks the narration continuously. Allocate each shot's timeline
    # by its spoken word count so its visual beat tracks the phrase being heard.
    total_words = sum(scene["_words"] for scene in clean_scenes)
    elapsed = 0.0
    for index, scene in enumerate(clean_scenes):
        start = elapsed
        end = 60.0 if index == len(clean_scenes) - 1 else elapsed + 60.0 * scene["_words"] / total_words
        scene["time"] = f"{start:.2f}-{end:.2f}s"
        del scene["_words"]
        elapsed = end

    narration = " ".join(scene["voice"] for scene in clean_scenes)
    voice = "en-ZA-LukeNeural"
    try:
        selected_language = Language(language)
        voice = EDGE_TTS_VOICENAME_MAPPING[selected_language]["male"]
    except Exception:
        pass
    asyncio.run(_make_voice(narration, voice, audio_path, language))
    if not audio_path.is_file() or audio_path.stat().st_size < 1000:
        raise RuntimeError("Narration audio could not be generated; refusing to create a silent video.")

    storyboard_path.write_text(
        json.dumps({"topic": topic, "scenes": clean_scenes}, ensure_ascii=False),
        encoding="utf-8",
    )
    blender = shutil.which("blender")
    if not blender:
        raise RuntimeError("The Blender 3D engine is not installed on this worker.")
    blender_script = Path(__file__).with_name("mechanical_cgi_render.py")
    if not blender_script.is_file():
        raise RuntimeError("The mechanical CGI Blender scene builder is missing from the worker deployment.")

    _set_job_stage("Starting mechanical CGI 3D render", 10)
    print(f"Starting mechanical CGI Blender render: {len(clean_scenes)} narration-timed shots, 8 fps, 540x960.")
    _run([
        blender, "-b", "--threads", "1", "--python", str(blender_script), "--",
        str(storyboard_path), str(silent_path),
    ], timeout=3600)
    if not silent_path.is_file() or silent_path.stat().st_size < 10000:
        raise RuntimeError("Blender completed without producing its 3D scene video.")

    _set_job_stage("Encoding vertical MP4", 88)
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(silent_path), "-i", str(audio_path),
        "-vf", "fps=24,scale=1080:1920:flags=lanczos,format=yuv420p",
        "-t", "60", "-map", "0:v:0", "-map", "1:a:0",
        "-af", "apad", "-c:v", "libx264", "-preset", "veryfast",
        "-threads", "2", "-crf", "19", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", str(output_path),
    ], timeout=900)
    _set_job_stage("Validating finished MP4", 98)
    _validate_mp4(output_path)
    return output_path


_RENDER_JOBS = {}
_RENDER_JOBS_LOCK = threading.Lock()
_JOB_CONTEXT = threading.local()
_RENDER_SEMAPHORE = threading.Semaphore(1)


def _set_job_stage(stage, progress):
    job_id = getattr(_JOB_CONTEXT, "job_id", None)
    if not job_id:
        return
    with _RENDER_JOBS_LOCK:
        job = _RENDER_JOBS.get(job_id)
        if job:
            job.update({"stage": stage, "progress": max(job.get("progress", 0), progress),
                        "updatedAt": time.time()})


def start_render_job(topic, language, token, storyboard_json):
    """Queue a render on a daemon thread and return immediately."""
    job_id = uuid.uuid4().hex
    with _RENDER_JOBS_LOCK:
        _RENDER_JOBS[job_id] = {
            "status": "queued", "progress": 0, "stage": "Queued",
            "videoUrl": None, "error": None, "createdAt": time.time(),
            "updatedAt": time.time()
        }

    def run():
        _JOB_CONTEXT.job_id = job_id
        try:
            with _RENDER_JOBS_LOCK:
                _RENDER_JOBS[job_id].update({"status": "queued", "stage": "Waiting for render slot",
                                             "progress": 1, "updatedAt": time.time()})
            # One Blender process at a time: this 1 GB worker cannot safely render concurrently.
            with _RENDER_SEMAPHORE:
                with _RENDER_JOBS_LOCK:
                    _RENDER_JOBS[job_id].update({"status": "rendering", "stage": "Preparing storyboard",
                                                 "progress": 3, "updatedAt": time.time()})
                video_url = render_video(topic, language, token, storyboard_json)
            with _RENDER_JOBS_LOCK:
                _RENDER_JOBS[job_id].update({
                    "status": "completed", "progress": 100, "stage": "Complete",
                    "videoUrl": video_url, "updatedAt": time.time()
                })
        except Exception as exc:
            print(f"Render job {job_id} failed: {type(exc).__name__}: {exc}", flush=True)
            with _RENDER_JOBS_LOCK:
                _RENDER_JOBS[job_id].update({
                    "status": "failed", "stage": "Failed",
                    "error": str(exc)[:1000], "updatedAt": time.time()
                })
        finally:
            try:
                del _JOB_CONTEXT.job_id
            except AttributeError:
                pass

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
