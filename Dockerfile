# Use a maintained Debian-based Python image.
FROM python:3.10-slim-bookworm

# Blender Workbench needs a headless-capable EGL/OpenGL stack even in background mode.
# Keep the Mesa software renderer so the worker does not require a GPU.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg fonts-dejavu-core blender \
        libegl1 libegl-mesa0 libgl1 libgl1-mesa-dri libglx0 libgbm1 \
        libx11-6 libxext6 libxrender1 libxi6 libxfixes3 libxkbcommon0 \
        libsm6 libxrandr2 libxinerama1 \
    && blender --background --factory-startup --python-expr "import bpy; s=bpy.context.scene; s.render.engine='BLENDER_WORKBENCH'; s.render.resolution_x=32; s.render.resolution_y=32; bpy.ops.render.render(write_still=False); print('BLENDER_WORKBENCH_RUNTIME_OK', bpy.app.version_string)" \
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
