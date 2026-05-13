# Multi-stage Dockerfile pro Gradio audio → noty pipeline.
# Cílová platforma: Azure Container Apps (Linux amd64 / arm64).

FROM python:3.11-slim AS base

# Systémové závislosti: MuseScore 4 (engraving), ffmpeg (audio-separator),
# libsndfile1 (soundfile), git (basic-pitch model fetch), build tools (madmom Cython).
RUN apt-get update && apt-get install -y --no-install-recommends \
    musescore3 \
    ffmpeg \
    libsndfile1 \
    libsm6 \
    libxext6 \
    libgl1 \
    libxkbcommon-x11-0 \
    libxcb-cursor0 \
    libdbus-1-3 \
    git \
    curl \
    build-essential \
    xvfb \
 && rm -rf /var/lib/apt/lists/*

# Install uv (rychlejší než pip).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Cache deps (vrstva)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Source code
COPY src/ ./src/
COPY app.py ./
COPY tests/fixtures/ ./tests/fixtures/

# Install project itself
RUN uv sync --frozen --no-dev

# Pre-download Basic Pitch ONNX model (jinak by se táhnul při prvním requestu)
RUN uv run python -c "from basic_pitch import ICASSP_2022_MODEL_PATH; print('Basic Pitch model:', ICASSP_2022_MODEL_PATH)"

# MuseScore na Linuxu se jmenuje "mscore3" nebo "mscore"; nastavíme MUSESCORE_PATH.
# Bullseye + later: balíček "musescore3" → /usr/bin/mscore3.
ENV MUSESCORE_PATH=/usr/bin/mscore3
# Xvfb pro headless MuseScore (potřebuje display i pro --convert).
ENV QT_QPA_PLATFORM=offscreen
ENV DISPLAY=:99

# Gradio
ENV GRADIO_SERVER_NAME=0.0.0.0
ENV GRADIO_SERVER_PORT=7860
EXPOSE 7860

CMD ["uv", "run", "python", "app.py"]
