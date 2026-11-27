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
    """Render continuous topic-specific 2.5D isometric animation from the authored scenes."""
    try:
        storyboard = json.loads(storyboard_json) if storyboard_json else {}
    except (TypeError, ValueError):
        raise ValueError("The scene storyboard is not valid JSON.")
    scenes = storyboard.get("scenes") if isinstance(storyboard, dict) else None
    if not isinstance(scenes, list) or not 3 <= len(scenes) <= 8:
        raise ValueError("Generate a storyboard with 3 to 8 scenes before rendering.")

    from PIL import Image, ImageDraw, ImageFont

    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = VIDEO_DIR / f"shorts-forge-{uuid.uuid4().hex}.mp4"
    font_path = next((item for item in FONT_CANDIDATES if Path(item).is_file()), None)
    if not font_path:
        raise RuntimeError("No usable font is installed for animated scene rendering.")

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
        components = [str(x).strip()[:36] for x in components[:3] if str(x).strip()]
        if len(components) < 2:
            raise ValueError(f"Scene {index + 1} needs at least two topic-specific moving components.")
        clean_scenes.append({
            "start": start, "end": end, "voice": voice_line,
            "visual": visual_line, "components": components,
            "camera": str(scene.get("camera") or "slow push-in").lower(),
            "sfx": str(scene.get("sfx") or "").strip(),
        })
        cursor = end

    if len(clean_scenes) < 3:
        raise ValueError("The storyboard does not contain enough complete scenes.")
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

    # Draw at 540x960 and upscale with a high-quality Lanczos filter to 1080x1920.
    # Every frame is newly drawn; this is continuous animation, not a slideshow.
    width, height, fps = 540, 960, 24
    accent_palette = [
        (56, 213, 200), (167, 139, 250), (245, 185, 66),
        (96, 165, 250), (251, 113, 133), (52, 211, 153)
    ]
    fonts = {
        "micro": ImageFont.truetype(font_path, 13),
        "small": ImageFont.truetype(font_path, 16),
        "medium": ImageFont.truetype(font_path, 20),
        "title": ImageFont.truetype(font_path, 27),
        "hero": ImageFont.truetype(font_path, 34),
    }

    def wrap_words(draw, text, font, max_width, max_lines=3):
        words = str(text).split()
        lines, current = [], ""
        for word in words:
            candidate = (current + " " + word).strip()
            if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = lines[-1][:max(1, len(lines[-1]) - 3)] + "..."
        return lines

    def draw_cube(draw, cx, cy, w, h, depth, label, accent, pulse):
        # Isometric top, left and right faces with a tactile lit edge.
        top = [(cx, cy - depth), (cx + w // 2, cy - depth // 2),
               (cx, cy), (cx - w // 2, cy - depth // 2)]
        left = [(cx - w // 2, cy - depth // 2), (cx, cy),
                (cx, cy + h), (cx - w // 2, cy + h - depth // 2)]
        right = [(cx, cy), (cx + w // 2, cy - depth // 2),
                 (cx + w // 2, cy + h - depth // 2), (cx, cy + h)]
        glow = tuple(min(255, int(c * (0.65 + 0.35 * pulse))) for c in accent)
        draw.polygon(left, fill=(22, 42, 61), outline=glow)
        draw.polygon(right, fill=(14, 29, 46), outline=glow)
        draw.polygon(top, fill=(38, 61, 81), outline=glow)
        draw.line(top + [top[0]], fill=glow, width=2)
        label_lines = wrap_words(draw, label.upper(), fonts["micro"], max(42, w // 2 - 12), 2)
        y = cy + 18
        for line in label_lines:
            box = draw.textbbox((0, 0), line, font=fonts["micro"])
            draw.text((cx - (box[2] - box[0]) / 2, y), line, font=fonts["micro"], fill=(235, 244, 250))
            y += 16

    def draw_arrow(draw, x1, y1, x2, y2, accent, phase):
        draw.line((x1, y1, x2, y2), fill=accent, width=3)
        import math
        angle = math.atan2(y2 - y1, x2 - x1)
        size = 9
        p1 = (x2, y2)
        p2 = (x2 - size * math.cos(angle - 0.55), y2 - size * math.sin(angle - 0.55))
        p3 = (x2 - size * math.cos(angle + 0.55), y2 - size * math.sin(angle + 0.55))
        draw.polygon([p1, p2, p3], fill=accent)

    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
        "-r", str(fps), "-i", "-", "-i", str(audio_path),
        "-vf", "scale=1080:1920:flags=lanczos,format=yuv420p",
        "-t", "60", "-map", "0:v:0", "-map", "1:a:0",
        "-af", "apad", "-c:v", "libx264", "-preset", "veryfast",
        "-threads", "2", "-crf", "19", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", str(output_path),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    total_frames = 60 * fps
    scene_index = 0
    try:
        for frame_number in range(total_frames):
            second = frame_number / fps
            while scene_index < len(clean_scenes) - 1 and second >= clean_scenes[scene_index]["end"]:
                scene_index += 1
            scene = clean_scenes[scene_index]
            scene_start, scene_end = scene["start"], scene["end"]
            phase = max(0.0, min(1.0, (second - scene_start) / max(0.1, scene_end - scene_start)))
            accent = accent_palette[scene_index % len(accent_palette)]
            import math

            image = Image.new("RGB", (width, height), (7, 11, 19))
            draw = ImageDraw.Draw(image, "RGBA")
            # Dark studio grid and subtle accent atmosphere.
            for gy in range(260, 730, 42):
                draw.line((28, gy, 512, gy), fill=(31, 47, 65, 90), width=1)
            for gx in range(35, 520, 48):
                draw.line((gx, 270, gx, 735), fill=(31, 47, 65, 55), width=1)
            draw.line((36, 125, 504, 125), fill=(50, 67, 88, 180), width=1)
            sweep_x = 36 + int((second * 120) % 468)
            draw.line((sweep_x, 123, min(504, sweep_x + 65), 123), fill=accent + (255,), width=3)

            draw.text((38, 48), "SHORTS FORGE  /  HOW IT WORKS", font=fonts["small"], fill=accent)
            draw.text((38, 155), f"{scene_index + 1:02d}  /  {len(clean_scenes):02d}", font=fonts["small"], fill=(141, 160, 182))
            title = scene["components"][0].upper()
            title_lines = wrap_words(draw, title, fonts["hero"], 450, 2)
            ty = 185
            for line in title_lines:
                draw.text((38, ty), line, font=fonts["hero"], fill=(244, 248, 252))
                ty += 42

            # Exploded components travel inward, then lock into an isometric mechanism.
            spread = 78 * (1.0 - phase) if any(k in scene["visual"].lower() for k in ("explod", "cutaway", "separat", "inside", "reveal")) else 24
            zoom = 1.0 + (0.10 * phase if "push" in scene["camera"] else 0.05 * (1.0 - phase))
            bob = math.sin(second * 2.4) * 5
            centers = [
                int(145 - spread + 8 * math.sin(second * 1.2)),
                int(270 + 4 * math.sin(second * 1.5 + 1)),
                int(395 + spread + 8 * math.sin(second * 1.1 + 2)),
            ]
            if "orbit" in scene["camera"] or "rotate" in scene["camera"]:
                centers = [int(x + 18 * math.sin(second * 1.4 + i * 2.1)) for i, x in enumerate(centers)]
            sizes = [(int(105 * zoom), int(88 * zoom), int(35 * zoom))] * 3
            # A central cutaway core pulses while the labelled components move around it.
            core_x, core_y = 270, 520 + int(bob)
            core_r = int(34 + 5 * math.sin(second * 3.2))
            draw.ellipse((core_x - core_r - 9, core_y - core_r - 9, core_x + core_r + 9, core_y + core_r + 9),
                         outline=accent + (105,), width=2)
            draw.ellipse((core_x - core_r, core_y - core_r, core_x + core_r, core_y + core_r),
                         fill=(17, 36, 52), outline=accent + (255,), width=3)
            draw.ellipse((core_x - 7, core_y - 7, core_x + 7, core_y + 7), fill=accent + (255,))

            for i, label in enumerate(scene["components"]):
                cx = centers[i] if i < len(centers) else centers[-1]
                w, h, depth = sizes[min(i, 2)]
                cy = core_y - 85 + (i % 2) * 150 + int(7 * math.sin(second * 2 + i))
                draw_cube(draw, cx, cy, w, h, depth, label, accent, 0.5 + 0.5 * math.sin(second * 2 + i))
                if i < 2:
                    draw_arrow(draw, cx + 44, cy + 35, core_x - 28 if cx < core_x else core_x + 28, core_y, accent, phase)

            # Moving energy/data particles make the mechanism visibly active every frame.
            for particle in range(9):
                px = int(45 + ((second * 92 + particle * 53) % 450))
                py = int(680 + 13 * math.sin(second * 3 + particle))
                radius = 2 + (particle % 3)
                draw.ellipse((px - radius, py - radius, px + radius, py + radius), fill=accent + (220,))

            # Scene-specific visual direction and exact spoken line are shown as captions.
            draw.rounded_rectangle((34, 765, 506, 910), radius=14, fill=(13, 23, 36, 238), outline=(48, 67, 88, 255), width=1)
            visual_lines = wrap_words(draw, scene["visual"], fonts["small"], 430, 3)
            vy = 784
            for line in visual_lines:
                draw.text((54, vy), line, font=fonts["small"], fill=(196, 210, 225))
                vy += 22
            caption_lines = wrap_words(draw, scene["voice"], fonts["micro"], 430, 2)
            cy = 858
            for line in caption_lines:
                draw.text((54, cy), line, font=fonts["micro"], fill=(244, 248, 252))
                cy += 17

            draw.line((36, 934, 504, 934), fill=(43, 58, 77, 255), width=3)
            draw.line((36, 934, 36 + int(468 * (second / 60)), 934), fill=accent + (255,), width=4)
            draw.text((38, 941), "60s  /  ISOMETRIC EXPLAINER", font=fonts["micro"], fill=(116, 136, 158))
            if process.stdin is None:
                raise RuntimeError("The video encoder input stream is unavailable.")
            process.stdin.write(image.tobytes())

        process.stdin.close()
        stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        return_code = process.wait(timeout=900)
        if return_code != 0:
            raise RuntimeError(f"FFmpeg animation encoding failed: {stderr[-1600:]}")
    except Exception:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise

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
