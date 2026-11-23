FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/app/data

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY kdbot ./kdbot
COPY login.py .

# Run as a normal user, not root
RUN useradd --create-home --uid 1000 bot && mkdir -p /app/data && chown bot /app/data
USER bot

CMD ["python", "-m", "kdbot.main"]
