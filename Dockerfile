# Lightweight AI-video job manager. LTX performs the generative rendering remotely; this worker assembles and validates MP4s.
FROM python:3.10-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg fonts-dejavu-core espeak-ng blender \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY forge-requirements.txt .
RUN pip install --no-cache-dir -r forge-requirements.txt

COPY . /app

# Fail the image build early if the API and CGI renderer have Python syntax errors; verify Blender starts headlessly.
RUN python -m py_compile /app/shortGPT/api_server.py /app/shortGPT/mechanical_cgi_render.py \
    && blender --background --threads 1 --python-expr "import bpy; print('SHORTS_FORGE_BLENDER_READY', bpy.app.version_string)"

# One Railway volume persists rendered videos.
RUN mkdir -p /app/.database/videos \
    && rm -rf /app/videos \
    && ln -s /app/.database/videos /app/videos

EXPOSE 31415

# Run only the lightweight Shorts Forge API; keep the legacy ShortGPT UI out of memory.
CMD ["sh", "-c", "mkdir -p /app/.database/videos && python -u ./forge_worker.py"]
