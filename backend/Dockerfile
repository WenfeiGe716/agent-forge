FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements*.txt ./
RUN pip install --no-cache-dir -r requirements-temporal.txt
COPY forge ./forge
EXPOSE 8000
CMD ["uvicorn","forge.api:app","--host","0.0.0.0","--port","8000","--workers","1"]
