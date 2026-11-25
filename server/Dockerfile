# Spatialise MCP server. Build from the repository root or from server/:
#   docker build -t spatialise-mcp server/
FROM python:3.13-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY spatialise_mcp ./spatialise_mcp
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin spatialise \
 && mkdir -p /data /etc/spatialise && chown spatialise /data
COPY --from=build /install /usr/local
COPY config.example.toml /etc/spatialise/config.toml
USER spatialise
# Mount your own config over /etc/spatialise/config.toml, and a volume at /data
# with SPATIALISE_STORE_PATH=/data/state.db so sign-ins survive a restart.
ENV SPATIALISE_CONFIG=/etc/spatialise/config.toml
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"
ENTRYPOINT ["spatialise-mcp"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
