#!/bin/bash
set -euo pipefail

rm -rf source
mkdir source
cd source
git init --quiet
git fetch --quiet --depth 1 "https://github.com/$REPOSITORY.git" "$COMMIT"
git checkout --quiet FETCH_HEAD
# Buildah finds the Containerfile or Dockerfile itself; Central checked the commit has one.
buildah build --layers --tag "app:$COMMIT" .
