FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN uv build --wheel --out-dir /dist

FROM python:3.12-slim
RUN useradd --create-home --uid 1000 hermetic
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
USER hermetic
WORKDIR /work
ENTRYPOINT ["hermetic"]
CMD ["--help"]
