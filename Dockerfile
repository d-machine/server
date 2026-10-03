FROM python:3.12-slim

WORKDIR /app

# requirements.txt installs arthdesk-db/arthdesk-instruments as editable
# sibling-directory packages (`-e ../arthdesk-db`), matching the
# arthdesk-system submodule layout on disk — so the build context is the
# arthdesk-system root (see docker-compose.yml's `context: ..`), and these
# land at /arthdesk-db, /arthdesk-instruments: siblings of /app, not inside it.
COPY arthdesk-db /arthdesk-db
COPY arthdesk-instruments /arthdesk-instruments

# Install dependencies first (cached layer — only rebuilds when requirements.txt changes)
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY server/app/ ./app/

# Create data directory for SQLite volume mount
RUN mkdir -p /app/data

EXPOSE 8000

# Init DB then start server
CMD ["sh", "-c", "python -m app.db_init && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
