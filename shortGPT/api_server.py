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
import tempfile
import uuid
from pathlib import Path

from urllib.parse import quote

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
        "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height",
        "-of", "default=noprint_wrappers=1", str(path),
    ], timeout=30)
    if "codec_name=h264" not in probe or "width=1080" not in probe or "height=1920" not in probe:
        raise RuntimeError("The rendered file is not a valid 1080x1920 H.264 vertical MP4.")
    if "codec_type=audio" not in probe:
        raise RuntimeError("The final MP4 has no narration/audio track.")
    duration = float(_run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ], timeout=30))
    if duration < 59.5 or duration > 60.5:
        raise RuntimeError(
            "The final MP4 must be exactly 60 seconds; the validated file is {:.2f} seconds.".format(duration)
        )
    return {"duration": round(duration, 2), "bytes": path.stat().st_size, "probe": probe}


def _cleanup_old_outputs():
    """Recover volume space without touching the newest preview videos."""
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    entries = list(VIDEO_DIR.iterdir())
    # Render jobs are serialized, so old work folders are always safe to remove.
    for item in entries:
        try:
            if item.is_dir() and item.name.startswith("job-"):
                # The render semaphore guarantees no other job is actively writing here.
                shutil.rmtree(item, ignore_errors=True)
            elif item.is_file() and item.suffix.lower() == ".mp4" and now - item.stat().st_mtime > 30 * 24 * 60 * 60:
                item.unlink(missing_ok=True)
        except OSError as exc:
            print(f"Output cleanup skipped for {item.name}: {exc}")

    usage = shutil.disk_usage(VIDEO_DIR)
    if usage.free < 180 * 1024 * 1024:
        completed = sorted(
            (p for p in VIDEO_DIR.glob("shorts-forge-*.mp4") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        # Retain the newest completed preview; older confirmed downloads can be removed when space is tight.
        while usage.free < 220 * 1024 * 1024 and len(completed) > 1:
            oldest = completed.pop(0)
            try:
                oldest.unlink(missing_ok=True)
                print(f"Removed old preview to recover render storage: {oldest.name}", flush=True)
            except OSError as exc:
                print(f"Could not remove old preview {oldest.name}: {exc}", flush=True)
            usage = shutil.disk_usage(VIDEO_DIR)

    if usage.free < 120 * 1024 * 1024:
        raise RuntimeError(
            f"Not enough render storage: only {usage.free // (1024 * 1024)} MB free. "
            "Download and remove older videos or increase the Railway volume."
        )


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
    """Generate narration entirely offline; no cloud TTS requests or usage charges."""
    local_voice = _local_voice_for(language) or "en"
    wav_path = audio_path.with_suffix(".wav")
    try:
        wav_path.unlink(missing_ok=True)
        subprocess.run(
            ["espeak-ng", "-v", local_voice, "-s", "145", "-w", str(wav_path), str(text)],
            capture_output=True, text=True, timeout=180, check=True
        )
        if not wav_path.is_file() or wav_path.stat().st_size < 1000:
            raise RuntimeError("Offline speech engine produced an empty WAV.")
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav_path),
             "-codec:a", "libmp3lame", "-q:a", "4", str(audio_path)],
            capture_output=True, text=True, timeout=120, check=True
        )
        if not audio_path.is_file() or audio_path.stat().st_size < 1000:
            raise RuntimeError("Offline speech conversion produced an empty audio file.")
        print("Narration generated locally with espeak-ng voice " + local_voice + ".", flush=True)
    finally:
        wav_path.unlink(missing_ok=True)


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


def start_render_job(topic, language, token, storyboard_json, ltx_api_key):
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
            # Serialize provider jobs so the worker does not overload memory or temporary storage.
            with _RENDER_SEMAPHORE:
                with _RENDER_JOBS_LOCK:
                    _RENDER_JOBS[job_id].update({"status": "rendering", "stage": "Preparing storyboard",
                                                 "progress": 3, "updatedAt": time.time()})
                video_url = render_video(topic, language, token, storyboard_json, ltx_api_key)
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



def _write_captions(scenes, path):
    lines = []
    for index, scene in enumerate(scenes):
        text = str(scene.get("voice") or scene.get("narration") or "").strip()
        if not text:
            continue
        start = index * 6
        end = start + 6
        def stamp(seconds):
            millis = int(round(seconds * 1000))
            hours, millis = divmod(millis, 3600000)
            minutes, millis = divmod(millis, 60000)
            secs, millis = divmod(millis, 1000)
            return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
        lines.extend([str(index + 1), stamp(start) + " --> " + stamp(end), text, ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def render_video(topic: str, language: str, token: str, storyboard_json: str = "", ltx_api_key: str = "") -> str:
    """Render a 60-second mechanical CGI Short locally, with no paid generation API."""
    expected_token = os.environ.get("SHORTGPT_API_TOKEN", "")
    if not expected_token or not hmac.compare_digest(str(token or ""), expected_token):
        raise ValueError("Invalid worker token.")
    topic = (topic or "").strip()
    if not topic or len(topic) > 600:
        raise ValueError("Enter a topic between 1 and 600 characters.")
    if not storyboard_json:
        raise ValueError("A scene-by-scene storyboard is required. Generate a script before rendering.")
    try:
        storyboard = json.loads(storyboard_json)
    except (TypeError, ValueError):
        raise ValueError("The scene storyboard is not valid JSON.")
    scenes = storyboard.get("scenes") if isinstance(storyboard, dict) else None
    if not isinstance(scenes, list) or len(scenes) != 10:
        raise ValueError("Generate exactly ten narration-matched scenes before rendering.")

    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_old_outputs()
    output_path = VIDEO_DIR / ("shorts-forge-" + uuid.uuid4().hex + ".mp4")
    work_dir = Path(tempfile.mkdtemp(prefix="shortsforge-cgi-"))
    silent_path = work_dir / "mechanical-cgi-silent.mp4"
    audio_path = work_dir / "narration.mp3"
    storyboard_path = work_dir / "storyboard.json"
    captions_path = work_dir / "captions.srt"
    temporary_final = work_dir / "finished.tmp.mp4"
    try:
        storyboard_path.write_text(json.dumps(storyboard, ensure_ascii=False), encoding="utf-8")
        _write_captions(scenes, captions_path)
        narration = " ".join(str(scene.get("voice") or scene.get("narration") or "").strip()
                             for scene in scenes).strip()
        if not narration:
            raise ValueError("The storyboard has no narration text.")
        _set_job_stage("Generating offline narration", 5)
        asyncio.run(_make_voice(narration, "", audio_path, language))

        _set_job_stage("Building animated mechanical CGI", 8)
        renderer = Path(__file__).with_name("mechanical_cgi_render.py")
        if not renderer.is_file():
            raise RuntimeError("The new mechanical CGI renderer is missing from the worker image.")
        blender = shutil.which("blender")
        if not blender:
            raise RuntimeError("Blender is not installed. The worker image must include Blender.")
        _run([
            blender, "--background", "--threads", "1", "--python", str(renderer), "--",
            "--topic", topic, "--storyboard", str(storyboard_path),
            "--output", str(silent_path), "--fps", "12", "--seconds", "60",
            "--width", "540", "--height", "960"
        ], timeout=7200)
        if not silent_path.is_file() or silent_path.stat().st_size < 10000:
            raise RuntimeError("Blender did not produce a valid silent CGI video.")

        _set_job_stage("Assembling narration, captions and final MP4", 88)
        subtitles_filter = "subtitles='" + str(captions_path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'") + "':force_style='FontName=DejaVu Sans,FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00101824,BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=100'"
        _run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(silent_path), "-i", str(audio_path),
            "-vf", subtitles_filter + ",scale=1080:1920:flags=lanczos,fps=24,format=yuv420p",
            "-af", "apad=pad_dur=60,atrim=0:60",
            "-t", "60", "-r", "24", "-c:v", "libx264", "-preset", "ultrafast",
            "-crf", "25", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-movflags", "+faststart", str(temporary_final)
        ], timeout=1800)
        if not temporary_final.is_file():
            raise RuntimeError("FFmpeg did not create the completed MP4.")
        _validate_mp4(temporary_final)
        os.replace(str(temporary_final), str(output_path))
        _validate_mp4(output_path)
        public_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
        if not public_domain:
            raise RuntimeError("Railway public domain is not configured.")
        return "https://" + public_domain + "/gradio_api/file=" + quote(str(output_path), safe="/")
    except Exception:
        output_path.unlink(missing_ok=True)
        temporary_final.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

