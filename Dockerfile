FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .
COPY api/ api/
COPY templates/ templates/
COPY static/ static/
COPY book/ book/

# Fly.io sets PORT=8080 by default; use it if set
ENV PORT=8080

EXPOSE 8080

CMD ["python", "app.py"]
