FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY services/embedding/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/embedding/app.py .
COPY runtime_security.py internal_auth.py .

EXPOSE 8001
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
