FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server.py watermark.py watermark.png watermark-preview.png ./
COPY dist ./dist
ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=10000
EXPOSE 10000
CMD ["python", "server.py"]
