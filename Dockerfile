FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --shell /bin/bash botuser \
    && mkdir -p /app/data \
    && chown -R botuser:botuser /app

USER botuser
ENV DB_PATH=/app/data/community_karma.db

CMD ["python", "run_bot.py"]
