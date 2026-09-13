FROM python:3.12-alpine

RUN apk add --no-cache ca-certificates tzdata \
    && adduser -D -u 1000 -h /app bot \
    && mkdir -p /data \
    && chown bot:bot /data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py handlers.py settings.py ./
USER bot
ENV PYTHONUNBUFFERED=1
CMD ["python", "main.py"]
