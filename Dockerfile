FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY forestmerge forestmerge
COPY api api
COPY web web
COPY config config
COPY ml ml
# данные образца и модели монтируются томами (см. docker-compose.yml)
EXPOSE 8765
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8765"]
