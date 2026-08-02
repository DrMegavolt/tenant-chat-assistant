FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY services/financing-agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/financing-agent/app.py .

EXPOSE 8003
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8003"]
