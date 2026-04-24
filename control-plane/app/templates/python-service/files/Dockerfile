FROM python:3.11-slim

WORKDIR /app

COPY {{source_path}} /app

RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 8080

CMD ["sh", "-c", "{{default_run_command}}"]

