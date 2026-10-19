#!/usr/bin/env bash
# Start ApplyStack and open it in your browser.
cd "$(dirname "$0")" && exec python3 server.py --open "$@"
