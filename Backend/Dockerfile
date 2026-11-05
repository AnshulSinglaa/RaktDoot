# One image for the API, the migration job and (later) the worker; the compose file picks the command.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# dependencies first: this layer is reused until requirements.txt changes (fast rebuilds)
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# never run as root: a code-execution bug then owns the container, not just the app
RUN useradd --system --no-create-home app
USER app

EXPOSE 8000
# ready = the app answers AND the database does (a 503 makes urlopen raise, so the check fails). No curl in -slim.
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/ready', timeout=2)" || exit 1
CMD ["uvicorn", "main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
