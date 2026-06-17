FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY api/ api/
COPY templates/ templates/
COPY static/ static/
COPY book/ book/

ENV PORT=7860
EXPOSE 7860

CMD ["python", "app.py"]
