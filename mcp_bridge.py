#!/usr/bin/env python3
"""Connect Claude Desktop to ApplyStack.

Claude Desktop starts local MCP servers and talks to them over stdin/stdout; this
script forwards those messages to the tracker's /mcp endpoint.

    python3 mcp_bridge.py https://www.applystack.app/mcp YOUR_MCP_TOKEN

(The Profile tab shows the exact Claude Desktop config to paste.)
"""
import json
import sys
import urllib.error
import urllib.request


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: mcp_bridge.py <tracker-url>/mcp <mcp-token>")
    url, token = sys.argv[1], sys.argv[2]
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "Authorization": f"Bearer {token}"}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=line.encode(), headers=headers),
                                        timeout=120) as res:
                body = res.read().decode("utf-8").strip()
        except (urllib.error.URLError, TimeoutError) as e:
            detail = e.read().decode("utf-8", "replace")[:300] if isinstance(e, urllib.error.HTTPError) else str(e)
            if isinstance(message, dict) and "id" in message:
                body = json.dumps({"jsonrpc": "2.0", "id": message["id"],
                                   "error": {"code": -32000, "message": f"ApplyStack: {detail}"}})
            else:
                continue
        if body:
            sys.stdout.write(body + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
