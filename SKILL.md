---
name: work-with-youtube
description: Work with YouTube in Claude Code using MCP captions and metadata first, saved Gemini material second, and new Gemini video requests only for what is still missing. Use for requests to inspect, summarize, query, compare, cite, translate, transcribe, or verify one or more YouTube URLs or videos.
---

# Work with YouTube

For each video, use sources in this order and stop once the task is answered:

1. YouTube MCP captions and metadata.
2. Saved Gemini material.
3. A new Gemini request, only for what is still missing.

## YouTube MCP

Use the native `mcp__youtube__*` tools, loading them with ToolSearch if they are
deferred. Prefer focused reads: `enhanced-transcript` with `filters.timeRange`
(seconds) reads part of a video, and `filters.search` finds a phrase.

Always pass `language`, set to the language spoken in the video. With no language
the server can return a dubbed track, and with a language that has no track it
silently returns a machine translation. Get the spoken language with:

```sh
node="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/mcp-servers/youtube/runtime/bin/node"
"$node" "${CLAUDE_SKILL_DIR}/scripts/save_captions.mjs" VIDEO_ID
```

Work from the spoken language even when it is not English. If the user wants
another language, translate from those captions yourself rather than asking
YouTube for a translation.

Captions can be fetched again at no cost, so do not store them for later use.
When the user wants a transcript kept, save it where they ask with the same
script, which writes timestamped paragraphs and never translates:

```sh
"$node" "${CLAUDE_SKILL_DIR}/scripts/save_captions.mjs" VIDEO_ID OUTPUT.md
```

If the script refuses, it names the captions that exist. Use a Gemini transcript,
or ask the user which captions to use.

If the tools are absent, get the user's permission, run this, and ask them to
exit and resume the session:

```sh
sh "${CLAUDE_SKILL_DIR}/scripts/setup_youtube_mcp.sh"
```

## Saved Gemini material

Reusable answers live in one folder per video:

```sh
ls "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/mcp-servers/youtube/state/VIDEO_ID"
```

Files are named `TYPE-START-END.EXT`, with times in seconds. `TYPE` is
`transcript` (`.json`), or `summary`, `visuals`, or `onscreen-text` (`.md`). If a
file covers the time you need, read it and do not call Gemini. A `.partial` file
is an incomplete answer; do not rely on it.

## Gemini

Gemini is the eyes and ears for what captions lack, such as a board, a slide, or
a video without captions. Ask it what is visible or audible, and do the reasoning
yourself.

Transcript of a clip, saved for reuse:

```sh
python3 "${CLAUDE_SKILL_DIR}/scripts/gemini_video.py" \
  --video-url URL --start 0 --end 600 --transcript \
  --output "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/mcp-servers/youtube/state/VIDEO_ID/transcript-0-600.json"
```

A one-off question prints its answer and saves nothing:

```sh
python3 "${CLAUDE_SKILL_DIR}/scripts/gemini_video.py" \
  --video-url URL --start 120 --end 180 --prompt "What is written on the board?"
```

For reusable material, add `--output` with the same naming, and save only what is
worth reusing.

The script tries the primary key, moves to the fallback when a key is rate
limited or rejected, and rests a rate-limited key until Gemini says it can be
used again. If every key is resting it reports the wait; do not retry sooner.

Long videos go in clips, one at a time: 600 seconds with a 4-second overlap for
transcripts, 1800 seconds otherwise. Start with one clip and continue only if it
works. If a clip fails or returns an incomplete answer, use a smaller clip, not
the same request again. Timestamps count from the start of the video.

Each call costs quota, so check the saved folder first and do not repeat a call
that succeeded.

## Keys

Never display, log, or commit a key, and never ask the user to paste one. Gemini
keys are in `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/secrets/work-with-youtube.env`.
Do not run `claude mcp get youtube`, which prints `YOUTUBE_API_KEY`; use
`claude mcp list`.

## Answering

Cite captions with timestamped YouTube links. Label what came from Gemini.
Separate observation from inference, and say when only part of a video was
processed.
