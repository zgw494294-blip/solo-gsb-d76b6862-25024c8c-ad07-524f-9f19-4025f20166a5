FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SESSIONS_DB=/data/sessions.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# A named volume mounted at /data inherits this ownership on first use, so the
# unprivileged runtime user can create and write the SQLite database.
RUN mkdir -p /data && chown nobody:nogroup /data

EXPOSE 8000

USER nobody

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
