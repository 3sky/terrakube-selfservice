FROM python:3.13-slim AS build
WORKDIR /src
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.13-slim
RUN useradd --uid 10001 --create-home app
COPY --from=build /install /usr/local
USER 10001
EXPOSE 8080
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
