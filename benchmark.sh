#!/bin/sh
set -eu
cd "$(dirname "$0")"
exec ./verify candidates/tempo --mode full --score-file score.json "$@"
