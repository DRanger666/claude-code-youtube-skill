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
GEMINI_API_KEY_LABEL=paid, project youtube-gemini
GEMINI_API_KEY_FALLBACK=...
GEMINI_API_KEY_FALLBACK_LABEL=free, old project
```

Only `GEMINI_API_KEY` is required. With one key, leave out both fallback
lines; to switch to another key, replace the value and its label. The next
request uses whatever the file holds.

A fallback adds capacity only if it comes from a different Google Cloud
project, since Gemini's quota is per project. It does not help when Gemini is
overloaded, since both keys reach the same model.

Labels are optional and not secret: they say where a key came from, such as its
account, project and tier. Each request's record names the keys it had, with
their labels and a fingerprint, the first 8 hex digits of the key's SHA-256,
which tells keys apart without revealing them. To find which record used a key
you hold, compute its fingerprint the same way without printing the key, for
example `read -rs key; printf %s "$key" | sha256sum | cut -c1-8`.

## Request records

Every Gemini request is recorded as one JSON line in
`~/.claude/mcp-servers/youtube/state/gemini-requests.jsonl` (mode `0600`), by
Claude Code and by the [web connector](https://github.com/DRanger666/claude-web-youtube-connector)
alike, since both run the same script. A record holds:

- the clip, the prompt, the model and settings, who asked (`claude-code` or the
  connector), and the caller's note on why it chose those settings
- the keys (name, label, fingerprint) and each attempt (key name, HTTP status,
  time, error)
- Gemini's finish reason, its `usageMetadata` exactly as returned, and its
  thought summary
- the path of the saved answer, or the error

The thought summary is Google's summary of the model's reasoning, not the
reasoning itself, which the API does not return; the thinking tokens are billed
in full all the same. For example, the token counts of the last ten requests:

```sh
tail -n 10 ~/.claude/mcp-servers/youtube/state/gemini-requests.jsonl \
  | jq -c '{id, outcome, finish_reason, usage: .usage | {promptTokenCount, thoughtsTokenCount, candidatesTokenCount}}'
```

## Development

```sh
python3 -m unittest discover -s tests
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md).
