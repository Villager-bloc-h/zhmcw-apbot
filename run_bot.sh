#!/bin/sh
# Run the autopatrol timeline bot (see README.md).
set -eu
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
if [ -d vendor ]; then
    export PYTHONPATH="$PWD/vendor${PYTHONPATH:+:$PYTHONPATH}"
fi
exec python3 ap_bot.py "$@"
