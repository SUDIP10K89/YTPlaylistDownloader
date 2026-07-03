# --- Base image ---
FROM python:3.12-slim

# ffmpeg: needed by yt-dlp for audio extraction / video merging
# curl, unzip, ca-certificates: needed to install Deno below
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Deno: yt-dlp's JS runtime for solving YouTube's "n challenge"
ENV DENO_INSTALL=/usr/local
RUN curl -fsSL https://deno.land/install.sh | sh
ENV PATH="/usr/local/bin:${PATH}"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Where downloaded files land before being served + deleted.
# Ephemeral is fine — files are removed right after each download is served.
RUN mkdir -p "/app/Single Audio" "/app/Single Videos" "/app/Playlist Audio" "/app/Playlist Videos"

EXPOSE 8000

# Render (and most PaaS platforms) inject a $PORT env var and route traffic
# to whatever port the app actually binds — hardcoding 8000 would break
# there. Falls back to 8000 for local `docker run`.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]