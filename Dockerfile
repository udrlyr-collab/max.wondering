FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Seoul

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && groupadd --gid 10001 moviemax \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin moviemax \
    && mkdir -p /data \
    && chown 10001:10001 /data

USER 10001:10001

ENTRYPOINT ["python", "-m", "moviemax"]
CMD ["console-worker"]
