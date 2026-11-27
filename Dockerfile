# Use a maintained Debian-based Python image.
FROM python:3.10-slim-bookworm

# Install FFmpeg without recommended extras and remove apt metadata.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Railway runs this worker without a CUDA GPU: install CPU-only PyTorch wheels.
RUN pip install --no-cache-dir torch==2.6.0 torchaudio==2.6.0 --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

EXPOSE 31415

# Do not print environment variables: they may contain API credentials.
CMD ["python", "-u", "./runShortGPT.py"]
