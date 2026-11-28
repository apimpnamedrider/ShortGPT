# Minimal CPU-based 3D renderer image. The Shorts Forge worker does not load the legacy Gradio UI or ML stack.
FROM python:3.10-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg fonts-dejavu-core espeak-ng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY forge-requirements.txt .
RUN pip install --no-cache-dir -r forge-requirements.txt

COPY . /app

# Fail the image build early if the API or mechanical CGI Blender script has Python syntax errors.
RUN python -m py_compile /app/shortGPT/api_server.py /app/shortGPT/ltx_video_engine.py

# One Railway volume persists rendered videos.
RUN mkdir -p /app/.database/videos \
    && rm -rf /app/videos \
    && ln -s /app/.database/videos /app/videos

EXPOSE 31415

# Run only the lightweight Shorts Forge API; keep the legacy ShortGPT UI out of memory.
CMD ["sh", "-c", "mkdir -p /app/.database/videos && python -u ./forge_worker.py"]
