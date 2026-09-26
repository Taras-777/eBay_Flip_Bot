FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Berlin \
    DATA_DIR=/data

WORKDIR /app

# Спершу залежності — цей шар кешується і не перезбирається при зміні коду
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Бот працює не від root; база й стан лежать у томі /data
RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /data && chown bot:bot /data
USER bot

CMD ["python", "main.py"]