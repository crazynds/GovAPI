#!/bin/sh
set -e

python -m app.migrate

exec "$@"
