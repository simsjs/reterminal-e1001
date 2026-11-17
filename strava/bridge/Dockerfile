FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY strava_bridge.py .
# /data holds the newest Strava refresh token (a named volume, so it survives rebuilds).
# Created here and owned by nobody so the volume starts out writable.
RUN mkdir /data && chown nobody /data
ENV PORT=8765 TOKEN_FILE=/data/strava_tokens.json
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health').status==200 else 1)"
USER nobody
CMD ["python", "strava_bridge.py"]
