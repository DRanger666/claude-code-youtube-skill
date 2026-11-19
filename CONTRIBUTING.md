# Contributing

Keep this simple. Add code only for a need that has actually come up, and prefer
removing code to guarding it.

- `SKILL.md` is the workflow, `scripts/` is the behavior, `tests/` checks it.
- Never commit keys, downloaded data, or generated responses.
- The [web connector](https://github.com/DRanger666/claude-web-youtube-connector)
  runs the installed `scripts/gemini_video.py` and imports
  `scripts/save_captions.mjs`, so their interfaces are shared with it: the
  Gemini script's options, its stderr (attempt lines, a summary line starting
  `request `, the error last), and the functions `save_captions.mjs` exports.
  Changing one means changing the connector too. Its startup check and
  `npm test` run this script's option parsing, so a mismatch shows there.

One change per commit, with an imperative subject. For anything non-trivial, the
message body says what changed, why, how it was validated, and its operational
impact, so the work can be understood a year later.
