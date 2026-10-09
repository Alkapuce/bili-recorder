FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y ffmpeg && apt-get clean
WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY recorder.py /app/recorder.py
COPY quark_upload.py /app/quark_upload.py
COPY session_store.py /app/session_store.py
COPY cloud_sync.py /app/cloud_sync.py
CMD ["python", "recorder.py"]
