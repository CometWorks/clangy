#!/bin/bash
# Builds the image and pushes it to the registry on the nas.
set -euo pipefail
cd "$(dirname "$0")/.."
IMAGE="192.168.1.40:5000/clangy2"
docker build -t "$IMAGE:latest" .
docker push "$IMAGE:latest"
echo "Pushed $IMAGE:latest"
