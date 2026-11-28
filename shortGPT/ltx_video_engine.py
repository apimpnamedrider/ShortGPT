"""LTX-powered generative video engine for Shorts Forge.

Creates ten narration-matched 6-second clips, carries the final frame forward
between clips for visual continuity, then assembles a 60-second 9:16 master.
"""
import asyncio
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


API_BASE = "https://api.ltx.io"
MODEL = "ltx-2-3-pro"
CLIP_SECONDS = 6
CLIP_COUNT = 10


def _json_request(url, api_key, payload=None, method=None, timeout=60):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method=method or ("POST" if body is not None else "GET"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError("LTX API returned HTTP {}: {}".format(exc.code, detail[-900:])) from exc
    except Exception as exc:
        raise RuntimeError("Could not contact the LTX video service: {}".format(exc)) from exc


def _upload_media(path, api_key, content_type):
    ticket = _json_request(API_BASE + "/v1/upload", api_key, method="POST")
    upload_url = ticket.get("upload_url")
    storage_uri = ticket.get("storage_uri")
    if not upload_url or not storage_uri:
        raise RuntimeError("LTX did not return a signed media-upload URL.")
    headers = {"Content-Type": content_type}
    headers.update(ticket.get("required_headers") or {})
    request = urllib.request.Request(
        upload_url,
        data=Path(path).read_bytes(),
        headers=headers,
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            if response.status not in (200, 201, 204):
                raise RuntimeError("LTX media upload returned HTTP {}.".format(response.status))
    except Exception as exc:
        raise RuntimeError("Could not upload a narration/reference file to LTX: {}".format(exc)) from exc
    return storage_uri


def _run(command, timeout=300):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        detail = (result.stderr or result.stdout or "No diagnostic output")[-1400:]
        raise RuntimeError("Media processing failed: " + detail)
    return result.stdout.strip()


def _probe_duration(path):
    value = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ], timeout=30)
    return float(value)


def _poll_generation(endpoint, job_id, api_key, progress, index):
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        status = _json_request(
            API_BASE + endpoint + "/" + job_id,
            api_key,
            method="GET",
            timeout=45,
        )
        state = str(status.get("status") or "").lower()
        if state == "completed":
            result = status.get("result") or {}
            video_url = result.get("video_url")
            if not video_url:
                raise RuntimeError("LTX marked clip {} complete without a video URL.".format(index + 1))
            return video_url
        if state == "failed":
            error = status.get("error") or status.get("message") or "Unknown generation error"
            raise RuntimeError("LTX clip {} failed: {}".format(index + 1, str(error)[:800]))
        if progress:
            progress(index + 1, CLIP_COUNT, "Generating cinematic AI clip {}/{}".format(index + 1, CLIP_COUNT))
        time.sleep(5)
    raise RuntimeError("LTX clip {} timed out after 20 minutes.".format(index + 1))


def _download(url, path):
    request = urllib.request.Request(url, headers={"User-Agent": "ShortsForge/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response, open(path, "wb") as output:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
    except Exception as exc:
        raise RuntimeError("Could not download the generated LTX clip: {}".format(exc)) from exc
    if not Path(path).is_file() or Path(path).stat().st_size < 10000:
        raise RuntimeError("LTX returned an empty or incomplete video clip.")


def _scene_prompt(topic, title, scene, index):
    parts = scene.get("components") or []
    return (
        "Create a premium, photorealistic 3D engineering CGI shot for an educational documentary. "
        "Subject/topic: " + str(topic) + ". Film title: " + str(title) + ". "
        "This is shot " + str(index + 1) + " of ten consecutive shots in one coherent film. "
        "VISUAL EVENT: " + str(scene.get("visual") or "") + " "
        "PHYSICAL DESIGN AND COMPOSITION: " + str(scene.get("illustration") or "") + " "
        "ACTUAL MOTION: " + str(scene.get("animation") or "") + " "
        "CAMERA: " + str(scene.get("camera") or "macro push-in") + ". " + str(scene.get("cameraDetail") or "") + " "
        "VISIBLE PHYSICAL COMPONENTS: " + ", ".join(str(x) for x in parts) + ". "
        "Narration being illustrated: " + str(scene.get("voice") or "") + ". "
        "Visual language: precision industrial product CGI, physically plausible construction, "
        "machined metal and realistic material response, fine bevels, screws, grooves, bearings, "
        "layered cutaway detail where relevant, controlled cinematic studio lighting, deep navy/black "
        "negative-space background, cool blue rim light and one restrained amber accent. "
        "Make the subject large and clearly readable in the vertical 9:16 frame; use a close or medium "
        "macro composition, not a distant tiny object. The visible action must match the narration. "
        "Maintain the same subject identity and design as the preceding shot/reference frame. "
        "No generic cubes, floating rectangles, placeholder blocks, diagram cards, flat illustrations, "
        "slideshow, stock footage, UI, logos, labels, captions, subtitles, watermarks, unrelated objects, "
        "or invented text. No people, no dialogue and no music; only subtle synchronized mechanical "
        "sound effects appropriate to the physical action."
    )[:4900]


def render_ltx_video(topic, language, work_dir, output_path, storyboard, api_key,
                    voice_name, voice_generator, progress=None):
    if not api_key or not api_key.strip():
        raise RuntimeError("The LTX video-generation API key is not connected. Add LTX_API_KEY in Shorts Forge settings.")
    scenes = storyboard.get("scenes") if isinstance(storyboard, dict) else None
    if not isinstance(scenes, list) or len(scenes) != CLIP_COUNT:
        raise RuntimeError("The generative CGI engine requires exactly ten narration-matched scenes.")
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    clips = []
    voice_files = []
    ambience_files = []
    previous_frame_uri = None
    title = str(storyboard.get("title") or topic)

    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            raise RuntimeError("Scene {} is not a valid object.".format(index + 1))
        voice_line = str(scene.get("voice") or "").strip()
        if not voice_line:
            raise RuntimeError("Scene {} has no narration.".format(index + 1))

        raw_audio = work_dir / "voice-{:02d}-raw.mp3".format(index + 1)
        fitted_audio = work_dir / "voice-{:02d}-6s.mp3".format(index + 1)
        asyncio.run(voice_generator(voice_line, voice_name, raw_audio, language))
        duration = _probe_duration(raw_audio)
        tempo = min(1.25, max(1.0, duration / 5.55))
        _run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw_audio),
            "-af", "atempo={:.4f},apad=pad_dur=6".format(tempo),
            "-t", "6", "-codec:a", "libmp3lame", "-q:a", "3", str(fitted_audio)
        ], timeout=90)
        fitted_duration = _probe_duration(fitted_audio)
        if fitted_duration < 5.8 or fitted_duration > 6.2:
            raise RuntimeError("Narration segment {} could not be fitted to six seconds.".format(index + 1))
        voice_files.append(fitted_audio)

        prompt = _scene_prompt(topic, title, scene, index)
        request_body = {
            "prompt": prompt,
            "resolution": "1080x1920",
            "duration": CLIP_SECONDS,
            "model": MODEL,
            "guidance_scale": 8.0,
        }
        # First shot is generated from text; later shots use the previous final frame
        # as an image reference so the machine and material identity stay coherent.
        endpoint = "/v2/text-to-video"
        if previous_frame_uri:
            request_body["image_uri"] = previous_frame_uri
            endpoint = "/v2/image-to-video"
        submitted = _json_request(API_BASE + endpoint, api_key, payload=request_body)
        job_id = str(submitted.get("id") or "")
        if not job_id:
            raise RuntimeError("LTX did not return a generation job ID for scene {}.".format(index + 1))
        if progress:
            progress(index + 1, CLIP_COUNT, "Submitted cinematic AI clip {}/{}".format(index + 1, CLIP_COUNT))
        video_url = _poll_generation(endpoint, job_id, api_key, progress, index)
        raw_clip = work_dir / "ltx-clip-{:02d}.mp4".format(index + 1)
        normalized = work_dir / "clip-{:02d}-1080x1920.mp4".format(index + 1)
        _download(video_url, raw_clip)

        _run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw_clip),
            "-an", "-vf", "fps=24,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
            "-t", "6", "-movflags", "+faststart", str(normalized)
        ], timeout=240)
        if _probe_duration(normalized) < 5.7:
            raise RuntimeError("Generated scene {} is shorter than its six-second target.".format(index + 1))
        clips.append(normalized)

        # Carry the preceding clip's final image into the next generation to stabilize identity.
        final_frame = work_dir / "continuity-{:02d}.jpg".format(index + 1)
        _run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-sseof", "-0.08",
            "-i", str(raw_clip), "-frames:v", "1", "-q:v", "2", str(final_frame)
        ], timeout=60)
        if final_frame.is_file() and final_frame.stat().st_size > 1000:
            previous_frame_uri = _upload_media(final_frame, api_key, "image/jpeg")

        # Preserve subtle generated sound design as a quiet bed; narration is restored exactly below.
        ambient = work_dir / "ambience-{:02d}.m4a".format(index + 1)
        try:
            _run([
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw_clip),
                "-vn", "-t", "6", "-c:a", "aac", "-b:a", "96k", str(ambient)
            ], timeout=60)
            if ambient.is_file() and ambient.stat().st_size > 1000:
                ambience_files.append(ambient)
        except Exception:
            pass
        if progress:
            progress(index + 1, CLIP_COUNT, "Validated AI clip {}/{}".format(index + 1, CLIP_COUNT))

    clip_list = work_dir / "clips.txt"
    clip_list.write_text("".join("file '{}'\n".format(str(p).replace("'", "'\\''")) for p in clips), encoding="utf-8")
    silent_master = work_dir / "visual-master.mp4"
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
        "-i", str(clip_list), "-c", "copy", "-an", "-movflags", "+faststart", str(silent_master)
    ], timeout=180)

    audio_list = work_dir / "voice-list.txt"
    audio_list.write_text("".join("file '{}'\n".format(str(p).replace("'", "'\\''")) for p in voice_files), encoding="utf-8")
    narration_master = work_dir / "narration-master.mp3"
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
        "-i", str(audio_list), "-c:a", "libmp3lame", "-q:a", "3", str(narration_master)
    ], timeout=120)

    srt = work_dir / "captions.srt"
    entries = []
    for index, scene in enumerate(scenes):
        start = index * CLIP_SECONDS
        end = start + CLIP_SECONDS
        def stamp(seconds):
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            whole = seconds % 60
            return "{:02d}:{:02d}:{:02d},000".format(hours, minutes, whole)
        entries.append("{}\n{} --> {}\n{}\n".format(index + 1, stamp(start), stamp(end), str(scene.get("voice") or "").strip()))
    srt.write_text("\n".join(entries), encoding="utf-8")

    ambient_master = work_dir / "ambience-master.m4a"
    has_ambience = len(ambience_files) == CLIP_COUNT
    if has_ambience:
        ambient_list = work_dir / "ambience-list.txt"
        ambient_list.write_text("".join("file '{}'\n".format(str(p).replace("'", "'\\''")) for p in ambience_files), encoding="utf-8")
        _run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
            "-i", str(ambient_list), "-c:a", "aac", "-b:a", "96k", str(ambient_master)
        ], timeout=120)

    subtitle_filter = "subtitles={}:force_style='FontName=DejaVu Sans,FontSize=18,PrimaryColour=&H00FFFFFF,OutlineColour=&H00101010,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=120'".format(str(srt))
    if has_ambience:
        audio_filter = "[1:a]volume=1.0[vo];[2:a]volume=0.14[fx];[vo][fx]amix=inputs=2:duration=first:dropout_transition=2[a]"
        audio_inputs = ["-i", str(narration_master), "-i", str(ambient_master)]
        audio_map = "[a]"
    else:
        audio_filter = None
        audio_inputs = ["-i", str(narration_master)]
        audio_map = "1:a:0"

    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(silent_master), *audio_inputs,
    ]
    if audio_filter:
        command.extend(["-filter_complex", audio_filter])
    command.extend([
        "-vf", subtitle_filter,
        "-map", "0:v:0", "-map", audio_map,
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-r", "24", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-t", "60",
        "-movflags", "+faststart", str(output_path)
    ])
    _run(command, timeout=600)
    if not output_path.is_file() or output_path.stat().st_size < 1_000_000:
        raise RuntimeError("LTX generation finished without a complete final MP4.")
    if progress:
        progress(CLIP_COUNT, CLIP_COUNT, "Final 60-second AI video assembled and validated")
    return output_path
