FROM python:3.12-alpine
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
COPY server.py smoke.py ./
EXPOSE 8080
CMD ["python3", "/app/server.py"]
