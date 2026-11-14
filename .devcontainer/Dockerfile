FROM mcr.microsoft.com/devcontainers/base:jammy
# Python 3.14 is supplied by the devcontainer feature.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libturbojpeg0 \
    && rm -rf /var/lib/apt/lists/*
