#!/bin/bash
set -e

echo "Installing C spatial dependencies..."
sudo apt-get update && sudo apt-get install -y \
    gdal-bin \
    libgdal-dev \
    libspatialindex-dev

echo "Installing python packages via uv..."
uv venv && uv sync

echo "Installing LID plugins for Claude..."
claude plugin marketplace add jszmajda/lid
claude plugin install linked-intent-dev@jszmajda-lid
claude plugin install arrow-maintenance@jszmajda-lid

