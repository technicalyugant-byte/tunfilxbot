#!/usr/bin/env bash
set -e

python -m pip install -r requirements.txt

if [ ! -x ".deno/bin/deno" ]; then
  curl -fsSL https://deno.land/install.sh | DENO_INSTALL="$PWD/.deno" sh
fi
