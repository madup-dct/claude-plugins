FROM docker.io/library/python:3.13.14-slim-trixie@sha256:bf503bb2243c5aad0aa951544dd60d165f992646441d35dea90893703fc26251

RUN useradd --no-create-home --home-dir /nonexistent --uid 10001 --shell /usr/sbin/nologin appuser

WORKDIR /app
COPY --chown=10001:10001 app.py /app/app.py

USER 10001:10001
EXPOSE 8080

CMD ["python", "/app/app.py"]
