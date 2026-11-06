# Claude Code YouTube Skill

This repository provides the `work-with-youtube` skill for local Claude Code
use. It gives Claude structured access to YouTube transcripts, metadata, audio and
visual evidence, translation, long-video processing, and timestamped answers.

It is the Claude Code counterpart of
[`codex-youtube-skill`](https://github.com/DRanger666/codex-youtube-skill), which
began as a ChatGPT Work skill. The history is shared, so the commits before
this repository's first Claude Code change explain how the routing, saved
material, and request logging came to be.

## Source order

The skill uses the least expensive sufficient source first:

1. Retrieve captions, transcript research, and metadata from the pinned YouTube
   MCP server.
2. Reuse compatible Gemini material already stored on the local machine.
3. Send only the unresolved video ranges or sensory questions to Gemini.

MCP results are temporary. Reusable Gemini responses, material indexes,
request logs, and router state are kept locally under the portable runtime's
`state/` directory. By default the runtime lives at
`${CLAUDE_CONFIG_DIR:-$HOME/.claude}/mcp-servers/youtube`; set `YOUTUBE_SKILL_HOME` to
override its parent directory. The skill has no Google Drive dependency.

## Local credentials

Store Gemini credentials once in
`${CLAUDE_CONFIG_DIR:-$HOME/.claude}/secrets/work-with-youtube.env`:

```text
GEMINI_API_KEY=...
GEMINI_API_KEY_FALLBACK=...
```

The `secrets/` directory must have mode `0700` and the credential file mode
`0600`. Never paste keys into a Claude Code conversation or store them in this
repository. If used, the fallback key belongs to a different Google Cloud
project and must differ from the primary. Explicit environment variables with
the same names remain available as temporary overrides.

## Reproducible YouTube MCP

The primary source is
[`coyaSONG/youtube-mcp-server`](https://github.com/coyaSONG/youtube-mcp-server),
pinned to:

- Version: `1.2.0`
- Commit: `06d5e7a83783f7a44498da88ade2ccaa42238747`
- Node.js: `v24.14.0`

The one-time setup script installs this exact upstream revision, builds it with
an isolated npm cache, verifies the MCP handshake, and registers it as the
user-scope `youtube` MCP in Claude Code. Claude Code then starts the stdio server
on demand; normal skill runs neither reinstall it nor use a separate client
wrapper.

`YOUTUBE_API_KEY` goes in the `env` block of the `youtube` server in
`~/.claude.json`. Add it by editing that file, because `claude mcp add --env`
would put the value in a command line. `claude mcp get youtube` prints that
value, so use `claude mcp list` to check the server.

## Gemini fallback

Gemini is used only when captions and saved local material do not satisfy the
request. It supplies missing visual or audible evidence; Claude performs the
final reasoning.

Long videos are processed in deterministic timestamp-bounded chunks. Reusable
responses are immutable and indexed by video, output type, format, and covered
time. Request logs prevent blind repetition after failures or interruptions.

## Install in Claude Code

Copy the runtime files into a personal skill directory. The installed skill
contains only `SKILL.md`, `references/`, and `scripts/`:

```sh
git clone https://github.com/DRanger666/claude-code-youtube-skill
cd claude-code-youtube-skill
skill=${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/work-with-youtube
mkdir -p "$skill"
cp -R SKILL.md references scripts "$skill/"
```

Then perform the machine setup once:

```sh
sh "$skill/scripts/setup_youtube_mcp.sh"
claude mcp list
```

This creates the pinned installation at
`${YOUTUBE_SKILL_HOME:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/mcp-servers}/youtube`
and registers it at user scope. If a matching registration already exists, setup
leaves it unchanged. Exit and resume the session afterward so that Claude Code
loads the new server. Changes to the skill's own files need no restart.

To reuse credentials from a Codex installation, copy the file without printing
it:

```sh
install -d -m 700 "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/secrets"
install -m 600 "${CODEX_HOME:-$HOME/.codex}/secrets/work-with-youtube.env" \
  "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/secrets/work-with-youtube.env"
```

## Development

Run the structural validator and unit tests before installation:

```sh
python3 /path/to/skill-creator/scripts/quick_validate.py .
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py' -v
```

[`REPOSITORY_MAP.md`](REPOSITORY_MAP.md) documents the maintained files and
their responsibilities.
