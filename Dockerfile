FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default: run the local launcher (overridden per-service in docker-compose.yml)
CMD ["python", "run.py"]
