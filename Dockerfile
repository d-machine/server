FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer — only rebuilds when requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/

# Create data directory for SQLite volume mount
RUN mkdir -p /app/data

EXPOSE 8000

# Init DB then start server
CMD ["sh", "-c", "python -m app.db_init && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
