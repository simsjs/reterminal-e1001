FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
# Each screen that keeps logins or history has its own named volume under /data.
# Created here and owned by nobody so new volumes start out writable.
RUN mkdir -p /data/dominion /data/strava /data/polestar && chown nobody /data/dominion /data/strava /data/polestar
ENV PORT=8765 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health').status==200 else 1)"
USER nobody
CMD ["python", "app.py"]
