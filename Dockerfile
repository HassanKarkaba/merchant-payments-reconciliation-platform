FROM python:3.13-slim@sha256:2d44738ebf4ce5f63b9d9ea871e9c7cfdc0426f7b0c97cac915fd88ded1d86bb AS dependencies
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && useradd --create-home --uid 10001 app
FROM dependencies AS runtime
COPY . /app
USER app
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000", "--noreload", "--insecure"]
FROM dependencies AS verify
COPY requirements-dev.lock /app/requirements-dev.lock
RUN pip install --no-cache-dir --require-hashes -r requirements-dev.lock && PLAYWRIGHT_BROWSERS_PATH=/opt/browsers python -m playwright install --with-deps chromium firefox webkit && chmod -R a+rX /opt/browsers
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/browsers
COPY . /app
USER app
