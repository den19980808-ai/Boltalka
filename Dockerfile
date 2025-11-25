# Базовый образ с Python
FROM python:3.12-slim

# Не задавать буферизацию вывода
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Рабочая директория
WORKDIR /app

# Системные зависимости (опционально, если нужны zoneinfo/ca-certificates)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*

# Копируем зависимости и устанавливаем
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Копируем исходники
COPY . /app

# Укажем часовой пояс в контейнере (опционально)
ENV TZ=Europe/Amsterdam

# Команда запуска: ваш основной файл
CMD ["python", "-u", "main.py"]
