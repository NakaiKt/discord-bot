#!/usr/bin/env bash
# Assemble the Lambda package into terraform/build/package (zipped by Terraform on apply).
# PyNaCl ships a compiled extension, so wheels are fetched for Lambda's platform
# (Linux x86_64, CPython 3.13) rather than for the machine running this script.
set -euo pipefail

cd "$(dirname "$0")"
PACKAGE_DIR="terraform/build/package"

rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"

python3 -m pip install \
  --requirement requirements.txt \
  --target "$PACKAGE_DIR" \
  --platform manylinux2014_x86_64 \
  --python-version 3.13 \
  --implementation cp \
  --only-binary=:all: \
  --quiet

cp src/*.py "$PACKAGE_DIR/"
echo "Built $PACKAGE_DIR"
