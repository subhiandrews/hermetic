# Base images are pinned by digest so published images change only through a repo change.
# Refresh the digests deliberately (docker buildx imagetools inspect <image>).
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
COPY --from=ghcr.io/astral-sh/uv:0.5@sha256:7bff3c3776ec467fc1437960f2c469d8beb30f536a6465a3350c647ccd260ec2 /uv /usr/local/bin/uv
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
# Build the wheel, and export the locked runtime dependencies (with hashes) so the image
# installs exactly what CI tested instead of re-resolving at release time.
RUN uv build --wheel --out-dir /dist \
 && uv export --locked --no-dev --no-emit-project --format requirements-txt -o /dist/requirements.txt

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
RUN useradd --create-home --uid 1000 hermetic
COPY --from=build /dist /tmp/dist
RUN pip install --no-cache-dir --require-hashes -r /tmp/dist/requirements.txt \
 && pip install --no-cache-dir --no-deps /tmp/dist/*.whl \
 && rm -rf /tmp/dist
USER hermetic
WORKDIR /work
ENTRYPOINT ["hermetic"]
CMD ["--help"]
