FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV EXECUTION_BACKEND=ea
ENV EA_SERVER_HOST=0.0.0.0
ENV EA_SERVER_PORT=19520
ENV API_PORT=8001

EXPOSE 8001 19520

CMD ["bash", "scripts/docker_entrypoint.sh"]
