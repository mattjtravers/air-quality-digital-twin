#!/bin/bash
# @spec OBS-ENV-004
set -e

echo "Installing C spatial dependencies..."
sudo apt-get update && sudo apt-get install -y \
    gdal-bin \
    libgdal-dev \
    libspatialindex-dev

echo "Installing python packages via uv..."
uv sync --all-groups

echo "Installing LID plugins for Claude..."
claude plugin marketplace add jszmajda/lid || true
claude plugin install linked-intent-dev@jszmajda-lid || true
claude plugin install arrow-maintenance@jszmajda-lid || true

echo "Waiting for PostGIS..."
for _ in $(seq 1 30); do
    if uv run python -c "from aqdt.observation_store.postgis import connect; connect().close()" 2>/dev/null; then
        break
    fi
    sleep 2
done

echo "Creating the PostGIS schema (apply_schema)..."
uv run python -c "from aqdt.observation_store.postgis import apply_schema, connect; apply_schema(connect())"

if [ -n "${AQDT_ARCHIVE_URI:-}" ]; then
    echo "Loading the archive from ${AQDT_ARCHIVE_URI} (rebuild)..."
    uv run python -c "from aqdt.observation_store.postgis import rebuild, connect; rebuild(connect(), None)"
else
    echo "AQDT_ARCHIVE_URI is not set; skipping rebuild. Set it as a Codespaces secret to come up populated."
fi
