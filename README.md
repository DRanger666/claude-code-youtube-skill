# Claude Code YouTube Skill

The `work-with-youtube` skill lets Claude Code read YouTube videos. It uses
captions and metadata from a YouTube MCP server first, and asks Gemini only about
what captions cannot supply, such as a slide, a board, or a video with no
captions.

It comes from [`codex-youtube-skill`](https://github.com/DRanger666/codex-youtube-skill),
which began as a ChatGPT Work skill. The shared history is kept here, and much of
what it built was later removed as more machinery than the job needed.

## Install

```sh
git clone https://github.com/DRanger666/claude-code-youtube-skill
cd claude-code-youtube-skill
skill=${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/work-with-youtube
mkdir -p "$skill"
cp -R SKILL.md scripts "$skill/"
sh "$skill/scripts/setup_youtube_mcp.sh"
```

Setup installs the pinned [YouTube MCP server](https://github.com/coyaSONG/youtube-mcp-server)
(version 1.2.0, commit `06d5e7a`, Node v24.14.0) under
`~/.claude/mcp-servers/youtube` and registers it at user scope. Exit and resume
the session to load it.

## Keys

`YOUTUBE_API_KEY` goes in the `env` block of the `youtube` server in
`~/.claude.json`. Edit the file directly, because `claude mcp add --env` would
put the key in a command line.

Gemini keys go in `~/.claude/secrets/work-with-youtube.env`, mode `0600`:

```text
GEMINI_API_KEY=...
GEMINI_API_KEY_FALLBACK=...
```

The fallback is optional. Gemini quota is per Google Cloud project, so it adds
capacity only if it comes from a different project than the primary. It does
not help when Gemini is overloaded, since both keys reach the same model.

## Request records

Every Gemini request is recorded as one JSON line in
`~/.claude/mcp-servers/youtube/state/gemini-requests.jsonl` (mode `0600`), by
Claude Code and by the [web connector](https://github.com/DRanger666/claude-web-youtube-connector)
alike, since both run the same script. A record holds the clip, the prompt, the
model and settings, each attempt (key name, HTTP status, time, error), Gemini's
finish reason, `usageMetadata` exactly as returned, and the path of the saved
answer. For example, the token counts of the last ten requests:

```sh
tail -n 10 ~/.claude/mcp-servers/youtube/state/gemini-requests.jsonl \
  | jq -c '{id, outcome, finish_reason, usage: .usage | {promptTokenCount, thoughtsTokenCount, candidatesTokenCount}}'
```

## Development

```sh
python3 -m unittest discover -s tests
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md).
