#!/usr/bin/env bash
# Start the Job Tracker and open it in your browser.
cd "$(dirname "$0")" && exec python3 server.py --open "$@"
