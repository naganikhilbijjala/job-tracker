"""A minimal MCP server (Streamable HTTP transport, stateless, JSON responses only).

Lets Claude (Claude Code, Claude Desktop via mcp_bridge.py, or claude.ai) read the
tracker's jobs, profile and tailored resumes, and save new resumes back.
"""
import json

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")


class ToolError(Exception):
    """Raised by a tool to report a problem back to the model (not a protocol error)."""


class Tool:
    def __init__(self, name, description, properties, required, fn):
        self.name, self.description, self.fn = name, description, fn
        self.schema = {"type": "object", "properties": properties, "required": required,
                       "additionalProperties": False}


def _result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle(message, tools, server_info, instructions=""):
    """Answer one JSON-RPC message. Returns None for notifications."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
    if "id" not in message:
        return None  # notifications (e.g. notifications/initialized) need no reply
    msg_id, method, params = message["id"], message["method"], message.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        return _result(msg_id, {
            "protocolVersion": requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": server_info,
            "instructions": instructions,
        })
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": [
            {"name": t.name, "description": t.description, "inputSchema": t.schema} for t in tools]})
    if method == "tools/call":
        tool = next((t for t in tools if t.name == params.get("name")), None)
        if tool is None:
            return _error(msg_id, -32602, f"Unknown tool: {params.get('name')}")
        try:
            output = tool.fn(**(params.get("arguments") or {}))
        except ToolError as e:
            return _result(msg_id, {"content": [{"type": "text", "text": str(e)}], "isError": True})
        except (TypeError, ValueError) as e:
            detail = str(e).split(") ", 1)[-1]  # drop the Python function name
            return _result(msg_id, {"content": [{"type": "text", "text": f"Invalid arguments for {tool.name}: {detail}"}],
                                    "isError": True})
        text = output if isinstance(output, str) else json.dumps(output, indent=1, ensure_ascii=False)
        return _result(msg_id, {"content": [{"type": "text", "text": text}]})
    return _error(msg_id, -32601, f"Method not found: {method}")
