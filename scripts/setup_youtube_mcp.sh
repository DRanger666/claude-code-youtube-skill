#!/bin/sh
# Install the pinned YouTube MCP server once and register it with Claude Code.
set -eu

REPOSITORY=https://github.com/coyaSONG/youtube-mcp-server.git
COMMIT=06d5e7a83783f7a44498da88ade2ccaa42238747
NODE_VERSION=v24.14.0

install=${CLAUDE_CONFIG_DIR:-$HOME/.claude}/mcp-servers/youtube
node=$install/runtime/bin/node
server=$install/app/dist/stdio-server.js

if [ ! -f "$server" ]; then
  echo "Installing the YouTube MCP server in $install" >&2
  rm -rf "$install/app" "$install/runtime"
  mkdir -p "$install/runtime" "$install/state"
  curl -fsSL "https://nodejs.org/dist/$NODE_VERSION/node-$NODE_VERSION-linux-x64.tar.xz" |
    tar -xJ --strip-components=1 -C "$install/runtime"
  git clone -q "$REPOSITORY" "$install/app"
  git -C "$install/app" checkout -q --detach "$COMMIT"
  (
    cd "$install/app"
    export PATH="$install/runtime/bin:$PATH"
    npm ci --no-audit --no-fund >&2
    npm run build >&2
  )
fi

# `claude mcp list` is used here because `claude mcp get` prints YOUTUBE_API_KEY.
if claude mcp list 2>/dev/null | grep -q '^youtube:'; then
  echo "A youtube MCP is already registered; it was left unchanged."
else
  claude mcp add --scope user youtube -- "$node" "$server"
  echo "Exit and resume the Claude Code session to load the YouTube MCP tools."
fi
