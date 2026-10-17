# TEPPA bot — лёгкий образ для VPS (Datapasa: 1 vCPU / 1 GB RAM).
# Python 3.12, как в .python-version; без тестов и .git в образе.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Непривилегированный пользователь — контейнер не ходит от root.
RUN addgroup --system app && adduser --system --ingroup app app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY bot/ ./bot/

USER app

EXPOSE 8080

# Проверка живости: GET /health → {"ok":true,...}
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"

CMD ["python", "main.py"]
