FROM python:3.12-slim

# ffmpeg renders the exported video; fonts-dejavu-core draws its captions.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 app

WORKDIR /srv
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/app ./backend/app
COPY frontend ./frontend

ENV NARRAFLOW_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
RUN mkdir -p /data && chown app:app /data
VOLUME ["/data"]
USER app
WORKDIR /srv/backend

# Secrets (ASSEMBLYAI_API_KEY, FIREWORKS_API_KEY) are injected at run time, never baked in:
#   docker run --env-file backend/.env -p 8000:8000 -v narraflow-data:/data narraflow
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
