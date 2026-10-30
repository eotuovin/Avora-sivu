# Small on purpose: the image is built on the shared production VPS.
FROM python:3.12-alpine

RUN adduser -D -H -u 10001 avora && mkdir -p /data && chown avora /data
WORKDIR /app
COPY server.py ./
COPY site ./site

USER avora
ENV PYTHONUNBUFFERED=1 AVORA_DB_PATH=/data/avora.db AVORA_PORT=8080
EXPOSE 8080
CMD ["python", "server.py"]
