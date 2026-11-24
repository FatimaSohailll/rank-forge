# RankForge: one container serves the agent API, the website, the admin dashboard and the article pages.
# Hugging Face Spaces (Docker SDK) expects port 7860 and a non-root user with uid 1000.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=7860

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt

COPY backend ./backend
COPY db ./db
COPY scripts ./scripts

RUN useradd -m -u 1000 app && chown -R app /app
USER app
WORKDIR /app/backend
EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"7860\")}/health')"

# --proxy-headers: trust X-Forwarded-For/Proto from the platform's proxy (rate limits use the client IP)
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
