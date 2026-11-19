# Telegram shop bot.
#
#   docker compose up -d --build        (see compose.yaml; needs a .env file)
#
# Everything the bot stores is in /app/data: keep that folder on a volume, or the
# shop's balances, products and orders are lost when the container is replaced.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 1000 --user-group bot
WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY bot.py ./
COPY shopbot ./shopbot
COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint
RUN chmod 755 /usr/local/bin/docker-entrypoint && mkdir -p /app/data && chown bot:bot /app/data

# The entrypoint makes the data folder writable, then runs the bot as the "bot" user.
ENTRYPOINT ["docker-entrypoint"]
CMD ["python", "bot.py"]
