FROM python:3.11-slim

WORKDIR /app
COPY server.py runtime_security.py index.html app.js admin.html admin.js styles.css README.md ./

ENV CHAT_HOST=0.0.0.0
ENV CHAT_PORT=8000
EXPOSE 8000

CMD ["python", "server.py"]
