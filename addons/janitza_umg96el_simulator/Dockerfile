ARG BUILD_FROM=python:3.12-alpine
FROM ${BUILD_FROM}

ARG BUILD_VERSION=0.2.0
ARG BUILD_ARCH=amd64

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

WORKDIR /app
COPY simulator.py /app/simulator.py

CMD ["python3", "/app/simulator.py"]
