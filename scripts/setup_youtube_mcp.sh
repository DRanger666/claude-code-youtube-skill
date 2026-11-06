#!/bin/sh
set -eu

MCP_NAME=youtube

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
claude_cli=${CLAUDE_MCP_CLI:-claude}

command -v "$claude_cli" >/dev/null 2>&1 || {
  echo "Missing required command: $claude_cli" >&2
  exit 1
}

install=$(sh "$script_dir/ensure_youtube_mcp.sh" "$@")
node=$install/runtime/bin/node
server=$install/app/dist/stdio-server.js

# `claude mcp get` prints the server's environment values, including
# YOUTUBE_API_KEY. Keep its output in a variable and never echo it.
registration_matches() {
  printf '%s\n' "$1" | rg -F "Command: $node" >/dev/null &&
    printf '%s\n' "$1" | rg -F "Args: $server" >/dev/null
}

if current=$("$claude_cli" mcp get "$MCP_NAME" 2>/dev/null); then
  if ! registration_matches "$current"; then
    echo "Claude Code already has a different MCP registration named '$MCP_NAME'." >&2
    echo "Inspect it with /mcp inside a session." >&2
    echo "Remove it deliberately, then rerun this setup if it should be replaced." >&2
    exit 1
  fi
  echo "Claude Code MCP '$MCP_NAME' is already registered to this installation."
else
  "$claude_cli" mcp add --scope user "$MCP_NAME" -- "$node" "$server"
  current=$("$claude_cli" mcp get "$MCP_NAME" 2>/dev/null) &&
    registration_matches "$current" || {
    echo "Claude Code did not record the expected '$MCP_NAME' registration." >&2
    exit 1
  }
  echo "Registered Claude Code MCP '$MCP_NAME' at user scope: $install"
  echo "Exit and resume the Claude Code session to make the YouTube MCP tools available."
fi
