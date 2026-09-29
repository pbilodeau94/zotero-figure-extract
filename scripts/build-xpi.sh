#!/usr/bin/env bash
# Build dist/zotero-figure-extract-<version>.xpi with manifest.json at the zip root.
set -euo pipefail
cd "$(dirname "$0")/.."
version=$(python3 -c 'import json;print(json.load(open("manifest.json"))["version"])')
out="dist/zotero-figure-extract-${version}.xpi"
mkdir -p dist
rm -f "$out"
zip -r -X "$out" manifest.json bootstrap.js prefs.js prefs.xhtml content LICENSE -x '*.DS_Store' >/dev/null
echo "Built $out"
unzip -l "$out"
