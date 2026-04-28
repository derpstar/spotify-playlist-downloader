FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY VERSION .

ENV SPOTIFY_DATA_DIR=/data
VOLUME ["/data"]

ENTRYPOINT ["python", "/app/app.py"]
