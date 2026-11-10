# Image used by DockerSandbox to run generated tests
# (src/gita/agents/test_generator/sandbox.py).
#
# The container runs with no network, so everything a test needs must be
# baked in here: pytest, plus the target repository's runtime
# dependencies. For a real repo, extend this image, e.g.
#
#   FROM gita-sandbox:latest
#   COPY requirements.txt /tmp/requirements.txt
#   USER root
#   RUN pip install --no-cache-dir -r /tmp/requirements.txt
#   USER 65534:65534
#
# and point TEST_GEN_SANDBOX_IMAGE at the result.
#
# Build:  docker build -f docker/sandbox.Dockerfile -t gita-sandbox:latest .
FROM python:3.13-slim

RUN pip install --no-cache-dir "pytest>=8,<9"

# Runs as nobody; DockerSandbox also passes --user 65534:65534.
USER 65534:65534
WORKDIR /work
