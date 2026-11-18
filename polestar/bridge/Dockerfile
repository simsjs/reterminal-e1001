FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY polestar_bridge.py .
# /data holds the self-calculated efficiency history (a named volume, so it survives rebuilds).
RUN mkdir /data && chown nobody /data
ENV PORT=8765 STATE_DIR=/data
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health').status==200 else 1)"
USER nobody
CMD ["python", "polestar_bridge.py"]
