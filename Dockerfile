FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y ffmpeg && apt-get clean
WORKDIR /app
COPY pyproject.toml /app/
COPY src /app/src
RUN pip install --no-cache-dir .
CMD ["bili-recorder", "run"]
