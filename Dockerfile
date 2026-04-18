FROM python:3.13-slim

# libgomp1 is needed by onnxruntime (used by fastembed).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY tools/ ./tools/
COPY scripts/ ./scripts/

ENV PYTHONPATH=/app
EXPOSE 8000

CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000"]
