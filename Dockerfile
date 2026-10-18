# Drawing2CAD MCP server (stdio) in a container.
# Build:  docker build -t drawing2cad .        (also runs the regression tests)
# Run:    docker run -i --rm --network none -v "$PWD/output:/output" -v "$PWD/drawings:/input:ro" drawing2cad
FROM python:3.12-slim

# OpenCascade (cadquery-ocp) links libGL, libX11 and libexpat even when headless
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libx11-6 libexpat1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY MCP/ ./MCP/
RUN chmod -R a+rX /app
# Build-time check: byte-compile everything and run the regression suite
# (all examples/*.json build + pass the model check, bad JSONs are rejected).
RUN python -m compileall -q MCP \
    && cd MCP && OUTPUT_DIR=/tmp/test_out python test_all.py

RUN useradd --create-home --uid 1000 cad \
    && mkdir -p /output /input && chown cad:cad /output
USER cad

ENV OUTPUT_DIR=/output \
    INPUT_DIR=/input \
    DRAWING2CAD_NOTIFY=0 \
    PYTHONUNBUFFERED=1
VOLUME ["/output"]

# MCP over stdio: Claude Desktop starts the container with `docker run -i`
ENTRYPOINT ["python", "/app/MCP/server.py"]
