FROM python:3.12-slim
WORKDIR /app
COPY app.py index.html ./
ENV PORT=8080 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health').status==200 else 1)"
# Runs as root only so it can read LaraPaper's files; both volumes are mounted read-only.
CMD ["python", "app.py"]
