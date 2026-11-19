FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir 'PyYAML>=6.0,<7' 'psycopg[binary]>=3.2,<4'
RUN useradd --uid 10001 --create-home registry
WORKDIR /app
COPY app.py bootstrap.py registry_files.py lifecycle.py pipeline.py planner.py board_setup.py intake.py /app/
COPY forms/ /app/forms/
USER 10001:10001
EXPOSE 8090
CMD ["python", "/app/app.py"]
