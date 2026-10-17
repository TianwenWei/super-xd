FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.nlp.lock.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.nlp.lock.txt \
    && useradd --uid 10001 --create-home nlp
COPY backend ./backend
COPY config/services.toml ./config/services.toml
COPY prompts ./prompts
USER 10001
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health/live', timeout=2)" || exit 1
CMD ["uvicorn", "backend.nlp_app:configured_app", "--factory", "--host", "0.0.0.0", "--port", "8001"]
