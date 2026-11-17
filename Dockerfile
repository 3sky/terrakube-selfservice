# Base image pinned by digest; Dependabot proposes updates.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS build
COPY requirements.lock /tmp/
# Only the locked, hash-checked wheels (no source builds); the app is copied, not built.
RUN pip install --no-cache-dir --prefix=/install --require-hashes --only-binary=:all: -r /tmp/requirements.lock

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
LABEL org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/3sky/terrakube-selfservice"
# Debian security fixes released after the base image was built (the scan fails on them).
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home app
COPY --from=build /install /usr/local
WORKDIR /srv
COPY app ./app
USER 10001
EXPOSE 8080
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
