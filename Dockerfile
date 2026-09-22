# ---------------------------------------------------------------------------
# Production image for the rollback demo.
#
# Key idea: the git SHA is baked in at BUILD time (ARG -> ENV). That is what
# makes an image immutable and identifiable — image tag abc1234 will always
# contain exactly the code of commit abc1234, forever. Rollback works because
# that old image is still sitting in ECR, already built and already tested.
# ---------------------------------------------------------------------------
FROM python:3.12-slim

# Keep Python quiet and predictable inside a container.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, in their own layer. Docker caches this layer, so editing
# app code rebuilds in seconds instead of re-downloading packages every time.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Now the application code.
COPY app/ ./app/

# CI passes --build-arg GIT_SHA=<7-char sha>. Locally it stays "local".
ARG GIT_SHA=local
ENV GIT_SHA=${GIT_SHA}

# Run as a non-root user. If the app is ever compromised, the attacker does not
# get root inside the container. AWS security reviews always ask about this.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# --timeout-graceful-shutdown gives in-flight requests 20 seconds to finish when
# ECS sends SIGTERM during a deploy or a rollback, so users never see a dropped
# connection. It must stay BELOW the task definition's stopTimeout (30s).
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--timeout-graceful-shutdown", "20"]
