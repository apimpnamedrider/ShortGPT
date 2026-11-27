# Use a maintained Debian-based Python image.
FROM python:3.10-slim-bookworm

# Install FFmpeg without recommended extras and remove apt metadata.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install CPU-only PyTorch wheels without resolving dependencies from the PyTorch index.
RUN pip install --no-cache-dir --no-deps torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir filelock typing-extensions sympy networkx jinja2 fsspec

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

# One Railway volume persists both the TinyMongo database and rendered videos.
RUN mkdir -p /app/.database/videos \
    && rm -rf /app/videos \
    && ln -s /app/.database/videos /app/videos

EXPOSE 31415

# Do not print environment variables: they may contain API credentials.
CMD ["sh", "-c", "mkdir -p /app/.database/videos && python -u ./runShortGPT.py"]
