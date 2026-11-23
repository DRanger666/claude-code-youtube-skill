#!/usr/bin/env python3
"""Ask Gemini about a clip of a YouTube video.

A question (--prompt) sends Gemini the YouTube link with the clip's range, so
it sees and hears the clip. A transcript (--transcript) needs only the sound:
the video's audio is downloaded once with yt-dlp into
~/.cache/work-with-youtube/audio/, the clip is cut from it with ffmpeg and sent
as audio, and the answer's timestamps are moved from the clip's start to the
video's. A download that fails is retried once after `uv tool upgrade yt-dlp`,
since YouTube's changes break older versions.

Tries GEMINI_API_KEY, then GEMINI_API_KEY_FALLBACK if the first key is rate
limited or rejected. A rate-limited key rests until Gemini says it can be used
again. When Gemini is overloaded or unreachable, it waits and retries with the
same key, since every key reaches the same model. Prints the answer, or writes
it to --output.

Every request is recorded, as one JSON line in
~/.claude/mcp-servers/youtube/state/gemini-requests.jsonl: the clip, the
settings, the keys (name, label and fingerprint, never the value), each
attempt, Gemini's finish reason and token counts as returned, its thought
summary, and where the answer went. stderr gets a line per attempt as it
happens and a summary at the end; both are made from the record, and the
summary carries its id.
"""

import argparse
import base64
import functools
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NamedTuple


# Defaults for the settings a caller can choose. Without --max-output-tokens
# the limit is the model's own maximum. gemini-4-hydrogen thinks at "high"
# unless told otherwise; "medium" is where we start, to adjust from records.
MODEL = "gemini-4-hydrogen"
THINKING_LEVEL = "medium"
# gemini-4-hydrogen's levels. It has no "minimal": Google's API rejects it, and
# OpenRouter silently runs it as "low" (checked 2026-11-21).
THINKING_LEVELS = ("low", "medium", "high", "xhigh")
TIMEOUT_SECONDS = 600
# Overload spikes last minutes, so the waits are longer than Google's SDK
# default (about 1 second, doubling to 60): 5, 10, 20, 40 seconds, plus jitter.
RETRIES = 4
FIRST_RETRY_SECONDS = 5
MAX_RETRY_SECONDS = 60
COOLDOWN_SECONDS = 60
TRANSIENT_STATUSES = {0, 408, 500, 502, 503, 504}
KEY_STATUSES = {401, 403, 429}
KEY_NAMES = ("GEMINI_API_KEY", "GEMINI_API_KEY_FALLBACK")
# Mono MP3 at 32 kbps transcribed as well as the full video in a test on
# 2026-11-20; audio costs the same 30 tokens a second at any bitrate.
AUDIO_BITRATE = "32k"
INSTALL = {
    "yt-dlp": "uv tool install 'yt-dlp[default]'",
    "ffmpeg": "sudo apt install ffmpeg",
}

TRANSCRIPT_PROMPT = (
    "TRANSCRIPT-ONLY MODE. Transcribe every audible human vocal passage containing "
    "linguistic content in the supplied audio. Preserve the words and repetitions "
    "as heard; do not summarize, paraphrase, translate, romanize, interpret, or "
    "correct them. Use the original language and its native script. Separate sung "
    "from spoken passages. Do not describe visuals, music, mood, story, themes, "
    "people, or actions. Do not add commentary. Every timestamp must be a string in "
    "fixed MM:SS.mmm format measured from the beginning of the supplied audio; for example, "
    "5 minutes 26.5 seconds must be written as 05:26.500, never as 526.5 or as "
    "elapsed seconds. The minute component must have at least two digits and may "
    "exceed 99 for long audio. Cover the complete supplied audio. Mark unclear "
    "words as uncertain rather than inventing them; use [inaudible] only when no "
    "reliable wording can be recovered. Return only the specified JSON structure."
)

TIMESTAMP_SCHEMA = {
    "type": "string",
    "pattern": r"^[0-9]{2,}:[0-5][0-9]\.[0-9]{3}$",
}

TRANSCRIPT_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "clip_start_timestamp": TIMESTAMP_SCHEMA,
        "clip_end_timestamp": TIMESTAMP_SCHEMA,
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "start_timestamp": TIMESTAMP_SCHEMA,
                    "end_timestamp": TIMESTAMP_SCHEMA,
                    "vocal_type": {
                        "type": "string",
                        "enum": ["sung", "spoken", "spoken_over_music", "other"],
                    },
                    # A free string came back as ben, bn, bne (another language), eng, en or
                    # English across requests; with this, bn or en every time (checked 2026-11-23).
                    "language": {
                        "type": "string",
                        "pattern": "^[a-z]{2}$",
                        "description": "The ISO 639-1 two-letter code of the segment's language, such as bn or en",
                    },
                    "text": {"type": "string"},
                    "audibility": {
                        "type": "string",
                        "enum": ["clear", "uncertain", "inaudible"],
                    },
                },
                "required": [
                    "start_timestamp",
                    "end_timestamp",
                    "vocal_type",
                    "language",
                    "text",
                    "audibility",
                ],
            },
        },
        "completed_through_timestamp": TIMESTAMP_SCHEMA,
        "transcription_complete": {"type": "boolean"},
        "truncation_detected": {"type": "boolean"},
    },
    "required": [
        "clip_start_timestamp",
        "clip_end_timestamp",
        "segments",
        "completed_through_timestamp",
        "transcription_complete",
        "truncation_detected",
    ],
}


class Key(NamedTuple):
    name: str
    value: str
    label: str | None


class GeminiError(Exception):
    """A request that ended without a complete answer; the message says why."""


def config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()


def state_dir():
    return config_dir() / "mcp-servers/youtube/state"


def load_keys():
    """Return the keys in order, from the environment or the secrets file.
    A key's optional label, NAME_LABEL, says where it came from, for records."""
    names = [name for key in KEY_NAMES for name in (key, f"{key}_LABEL")]
    values = {name: os.environ[name] for name in names if name in os.environ}
    if not any(name in values for name in KEY_NAMES):
        path = config_dir() / "secrets/work-with-youtube.env"
        if not path.exists():
            raise GeminiError(f"Missing Gemini credentials file: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    keys = [Key(name, values[name], values.get(f"{name}_LABEL") or None) for name in KEY_NAMES if values.get(name)]
    if not keys:
        raise GeminiError("Missing required Gemini credential: GEMINI_API_KEY")
    return keys


def fingerprint(value):
    """A short hash that tells keys apart in records without revealing them."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def video_id(url):
    """The 11-character ID from a YouTube link of any usual form, or a bare ID;
    None if there is none. Anything else in the link, such as a time, is
    dropped: Gemini given a link with a time in it lost the video and billed
    its page as text."""
    url = url.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return url
    parsed = urllib.parse.urlparse(url if "//" in url else f"https://{url}")
    host = (parsed.hostname or "").removeprefix("www.").removeprefix("m.")
    path = parsed.path.strip("/").split("/")
    if host == "youtu.be":
        candidate = path[0]
    elif host in ("youtube.com", "music.youtube.com") and path[0] == "watch":
        candidate = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
    elif host == "youtube.com" and path[0] in ("shorts", "live", "embed") and len(path) > 1:
        candidate = path[1]
    else:
        return None
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else None


def watch_url(video):
    return f"https://www.youtube.com/watch?v={video}"


def cache_dir():
    return Path(os.environ.get("XDG_CACHE_HOME") or "~/.cache").expanduser() / "work-with-youtube/audio"


def run(command):
    return subprocess.run(command, capture_output=True, timeout=TIMEOUT_SECONDS)


def program(name):
    path = shutil.which(name)
    if path is None:
        raise GeminiError(f"{name} is not installed; install it with: {INSTALL[name]}")
    return path


def last_line(process):
    lines = process.stderr.decode("utf-8", "replace").strip().splitlines()
    return lines[-1] if lines else f"exit status {process.returncode}"


def download_audio(video, audio, run=run):
    """Return the video's whole audio track, downloading it into the cache the
    first time. A failed download is tried once more after upgrading yt-dlp."""
    cached = sorted(cache_dir().glob(f"{video}.*"))
    audio["downloaded"] = not cached
    if cached:
        return cached[0]
    yt_dlp = program("yt-dlp")
    version = lambda: run([yt_dlp, "--version"]).stdout.decode().strip()
    audio["yt_dlp"] = version()
    cache_dir().mkdir(parents=True, exist_ok=True)
    for upgraded in (False, True):
        # Into a folder of its own, then moved into place whole, so a download
        # that stops halfway never looks like a cached one.
        with tempfile.TemporaryDirectory(dir=cache_dir(), prefix=".download-") as folder:
            process = run([yt_dlp, "--quiet", "--no-warnings", "--no-playlist", "--js-runtimes", "node",
                           "-f", "bestaudio", "-o", f"{folder}/{video}.%(ext)s", watch_url(video)])
            files = list(Path(folder).glob(f"{video}.*"))
            if process.returncode == 0 and files:
                return Path(shutil.move(files[0], cache_dir() / files[0].name))
        error = last_line(process)
        uv = shutil.which("uv")
        if upgraded or uv is None:
            break
        run([uv, "tool", "upgrade", "yt-dlp"])
        new_version = version()
        if new_version == audio["yt_dlp"]:
            break
        audio["upgraded"] = {"from": audio["yt_dlp"], "to": new_version}
        audio["yt_dlp"] = new_version
    raise GeminiError(f"yt-dlp {audio['yt_dlp']} could not download the audio: {error}")


def cut_audio(source, start, end, run=run):
    """The clip from start to end of the audio file, as mono MP3 bytes."""
    process = run([program("ffmpeg"), "-v", "error", "-ss", str(start), "-t", str(end - start), "-i", str(source),
                   "-vn", "-ac", "1", "-b:a", AUDIO_BITRATE, "-f", "mp3", "pipe:1"])
    if process.returncode != 0:
        raise GeminiError(f"ffmpeg could not cut the clip: {last_line(process)}")
    if not process.stdout:
        raise GeminiError(f"The clip from {start}s is past the end of the video")
    return process.stdout


def shift_timestamps(text, seconds):
    """Move a transcript's timestamps by seconds, from the clip's start to the
    video's; None if the answer is not the expected JSON."""
    def shift(stamp):
        minutes, rest = stamp.split(":")
        milliseconds = round((int(minutes) * 60 + float(rest) + seconds) * 1000)
        return f"{milliseconds // 60000:02d}:{milliseconds % 60000 // 1000:02d}.{milliseconds % 1000:03d}"
    try:
        transcript = json.loads(text)
        for field in ("clip_start_timestamp", "clip_end_timestamp", "completed_through_timestamp"):
            transcript[field] = shift(transcript[field])
        for segment in transcript["segments"]:
            segment["start_timestamp"] = shift(segment["start_timestamp"])
            segment["end_timestamp"] = shift(segment["end_timestamp"])
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return json.dumps(transcript, ensure_ascii=False, indent=2)


def video_part(url, start, end):
    return {
        "fileData": {"fileUri": url, "mimeType": "video/*"},
        "videoMetadata": {"startOffset": f"{start}s", "endOffset": f"{end}s"},
    }


def audio_part(clip):
    return {"inlineData": {"mimeType": "audio/mp3", "data": base64.b64encode(clip).decode("ascii")}}


def build_request(media, prompt, thinking_level=None, max_output_tokens=None):
    # Thought summaries come back as parts marked "thought"; they are kept in
    # the request record, to see how Gemini went about a clip.
    config = {"thinkingConfig": {"includeThoughts": True}}
    if thinking_level:
        config["thinkingConfig"]["thinkingLevel"] = thinking_level.upper()
    if max_output_tokens:
        config["maxOutputTokens"] = max_output_tokens
    if prompt is None:
        prompt = TRANSCRIPT_PROMPT
        config["responseMimeType"] = "application/json"
        config["responseJsonSchema"] = TRANSCRIPT_RESPONSE_SCHEMA
    return {
        "contents": [{"role": "user", "parts": [media, {"text": prompt}]}],
        "generationConfig": config,
    }


def post(key, body, model=MODEL):
    """Send one request and return (HTTP status, decoded JSON)."""
    request = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.load(error)
        except json.JSONDecodeError:
            return error.code, {}
    except (urllib.error.URLError, TimeoutError) as error:
        return 0, {"error": {"message": type(error).__name__}}


def retry_delay(payload):
    """Return the seconds Gemini asked us to wait, or the default."""
    for detail in payload.get("error", {}).get("details", []):
        match = re.fullmatch(r"([0-9.]+)s", str(detail.get("retryDelay", "")))
        if match:
            return float(match[1])
    return COOLDOWN_SECONDS


def log_to_stderr(line):
    print(line, file=sys.stderr, flush=True)


def call_gemini(keys, body, cooldown_file, attempts, send=post, sleep=time.sleep, now=time.time, log=log_to_stderr):
    """Return the first successful response, moving on from keys that fail or
    are resting. Appends each attempt to attempts, and logs it."""
    cooldowns = json.loads(cooldown_file.read_text()) if cooldown_file.exists() else {}
    status, message = None, ""
    for name, key, _ in keys:
        if cooldowns.get(name, 0) > now():
            log(f"{name} skipped: resting for {cooldowns[name] - now():.0f} more seconds")
            continue
        for retry in range(RETRIES + 1):
            started = now()
            status, payload = send(key, body)
            message = "" if status == 200 else payload.get("error", {}).get("message", "")
            attempts.append({"key": name, "status": status, "seconds": round(now() - started, 1)})
            if message:
                attempts[-1]["error"] = message
            log(attempt_line(len(attempts), attempts[-1]))
            if status == 200:
                return payload
            if status in TRANSIENT_STATUSES and retry < RETRIES:
                sleep(min(FIRST_RETRY_SECONDS * 2**retry, MAX_RETRY_SECONDS) + random.uniform(0, 1))
                continue
            if status in TRANSIENT_STATUSES:
                raise GeminiError(
                    f"Gemini overloaded or unreachable ({status}) after {RETRIES + 1} attempts: "
                    f"{message}; try again in a few minutes"
                )
            if status == 429:
                cooldowns[name] = now() + retry_delay(payload)
                cooldown_file.parent.mkdir(parents=True, exist_ok=True)
                cooldown_file.write_text(json.dumps(cooldowns))
            bad_key = status == 400 and "api key" in message.lower()
            if status in KEY_STATUSES or bad_key:
                break
            raise GeminiError(f"Gemini request failed ({status}): {message}")
    if status is None or status == 429:
        wait = min(until for until in cooldowns.values() if until > now()) - now()
        raise GeminiError(f"Every Gemini key is cooling down; try again in {wait:.0f} seconds")
    raise GeminiError(f"Every Gemini key failed; last error ({status}): {message}")


def record_response(record, response):
    """Copy what Gemini said about its answer into the record, and return the
    answer. Thought parts are marked "thought": true; they go to the record,
    never into the answer."""
    candidate = (response.get("candidates") or [{}])[0]
    record["finish_reason"] = candidate.get("finishReason", "UNKNOWN")
    record["usage"] = response.get("usageMetadata", {})
    for field, name in [("modelVersion", "model_version"), ("responseId", "response_id"), ("promptFeedback", "prompt_feedback")]:
        if field in response:
            record[name] = response[field]
    parts = candidate.get("content", {}).get("parts", [])
    record["thoughts"] = "".join(part.get("text", "") for part in parts if part.get("thought"))
    return "".join(part.get("text", "") for part in parts if not part.get("thought"))


def parse_arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video-url", required=True)
    parser.add_argument("--start", required=True, type=int, help="seconds")
    parser.add_argument("--end", required=True, type=int, help="seconds")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--transcript", action="store_true")
    mode.add_argument("--prompt")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--thinking-level", choices=THINKING_LEVELS, default=THINKING_LEVEL,
                        help="which levels a model accepts differs; Gemini rejects others")
    parser.add_argument("--max-output-tokens", type=int, help="thinking and answer together")
    parser.add_argument("--note", help="why these settings, kept in the request record")
    parser.add_argument("--caller", default="claude-code", help="who asked, kept in the request record")
    args = parser.parse_args()
    if not 0 <= args.start < args.end:
        parser.error("Require 0 <= start < end")
    if args.max_output_tokens is not None and args.max_output_tokens < 1:
        parser.error("--max-output-tokens must be positive")
    args.video = video_id(args.video_url)
    if args.video is None:
        parser.error(f"Not a YouTube video link or ID: {args.video_url}")
    return args


def new_record(args):
    """The request as asked; ask() adds what happened."""
    return {
        "id": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(2),
        "video": watch_url(args.video),
        "start": args.start,
        "end": args.end,
        "mode": "prompt" if args.prompt else "transcript",
        "prompt": args.prompt,
        "model": args.model,
        "thinking_level": args.thinking_level,
        "max_output_tokens": args.max_output_tokens,
        "caller": args.caller,
        "note": args.note,
        "attempts": [],
    }


def ask(args, record):
    """Send the request, save the answer, and fill in the record; raise
    GeminiError if there is no complete answer."""
    keys = load_keys()
    record["keys"] = {key.name: {"label": key.label, "fingerprint": fingerprint(key.value)} for key in keys}
    if args.prompt is None:
        audio = record["audio"] = {}
        source = download_audio(args.video, audio)
        clip = cut_audio(source, args.start, args.end)
        audio.update(source=str(source), clip_bytes=len(clip))
        media = audio_part(clip)
    else:
        media = video_part(watch_url(args.video), args.start, args.end)
    body = build_request(media, args.prompt, args.thinking_level, args.max_output_tokens)
    send = functools.partial(post, model=record["model"])
    response = call_gemini(keys, body, state_dir() / "gemini-cooldowns.json", record["attempts"], send=send)
    text = record_response(record, response)
    if args.prompt is None:
        # Gemini heard only the clip; an answer that is not the expected JSON,
        # such as a cut-off one, keeps the clip's timestamps, and says so here.
        shifted = shift_timestamps(text, args.start)
        record["audio"]["timestamps_from"] = "video" if shifted else "clip"
        text = shifted or text
    complete = record["finish_reason"] == "STOP"
    if args.output is None:
        print(text)
    else:
        # An incomplete answer is kept, but never under a reusable name.
        output = args.output if complete else Path(f"{args.output}.partial")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
        record["output"] = str(output.resolve())
        print(output)
    if not complete:
        advice = "; use a smaller clip, a lower thinking level or a higher output limit"
        raise GeminiError(f"Incomplete answer (finishReason {record['finish_reason']})"
                          + (advice if record["finish_reason"] == "MAX_TOKENS" else ""))


def save_record(record, path):
    """Append the record as one line. A single write to a file opened for
    appending lands whole, so requests running at once do not mix lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(descriptor, line)
    finally:
        os.close(descriptor)


def attempt_line(number, attempt):
    line = f"attempt {number} with {attempt['key']}: {attempt['status']} in {attempt['seconds']:.1f}s"
    return line + (f": {attempt['error'][:200]}" if "error" in attempt else "")


def summary_line(record):
    """One line for people: how the request ended, its settings and its token
    counts. Gemini leaves out counts that are zero."""
    line = (
        f"request {record['id']} {record['outcome']} in {record['seconds']:.1f}s; {record['model']}, "
        f"thinking {record['thinking_level'] or 'model default'}, "
        f"output limit {record['max_output_tokens'] or 'model maximum'}"
    )
    if "finish_reason" in record:
        usage = record["usage"]
        by_kind = ", ".join(
            f"{detail.get('modality', '?').lower()} {detail.get('tokenCount', 0)}"
            for detail in usage.get("promptTokensDetails", [])
        )
        line += (
            f"; finish {record['finish_reason']}; tokens: input {usage.get('promptTokenCount', 0)}"
            + (f" ({by_kind})" if by_kind else "")
            + f", thinking {usage.get('thoughtsTokenCount', 0)}"
            + f", answer {usage.get('candidatesTokenCount', 0)}"
            + f", total {usage.get('totalTokenCount', 0)}"
        )
    return line


def stop(signum, frame):
    raise GeminiError("Stopped by the caller before Gemini answered")


def main():
    args = parse_arguments()
    # A caller that gives up sends SIGTERM; the request is still recorded.
    signal.signal(signal.SIGTERM, stop)
    record = new_record(args)
    started = time.time()
    try:
        ask(args, record)
        record["outcome"] = "ok"
    except GeminiError as error:
        record["outcome"] = "incomplete" if "finish_reason" in record else "failed"
        record["error"] = str(error)
    record["seconds"] = round(time.time() - started, 1)
    save_record(record, state_dir() / "gemini-requests.jsonl")
    log_to_stderr(summary_line(record))
    if "error" in record:
        sys.exit(record["error"])


if __name__ == "__main__":
    main()
