#!/usr/bin/env python3
"""Ask Gemini about a clip of a YouTube video.

Tries GEMINI_API_KEY, then GEMINI_API_KEY_FALLBACK if the first key is rate
limited or rejected. A rate-limited key rests until Gemini says it can be used
again. Prints the answer, or writes it to --output.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/"
    "models/gemini-3.6-flash:generateContent"
)
MAX_OUTPUT_TOKENS = 8192
TIMEOUT_SECONDS = 600
RETRIES = 2
COOLDOWN_SECONDS = 60
TRANSIENT_STATUSES = {0, 408, 500, 502, 503, 504}
KEY_STATUSES = {401, 403, 429}
KEY_NAMES = ("GEMINI_API_KEY", "GEMINI_API_KEY_FALLBACK")

TRANSCRIPT_PROMPT = (
    "TRANSCRIPT-ONLY MODE. Transcribe every audible human vocal passage containing "
    "linguistic content in the supplied interval. Preserve the words and repetitions "
    "as heard; do not summarize, paraphrase, translate, romanize, interpret, or "
    "correct them. Use the original language and its native script. Separate sung "
    "from spoken passages. Do not describe visuals, music, mood, story, themes, "
    "people, or actions. Do not add commentary. Every timestamp must be a string in "
    "fixed MM:SS.mmm format measured from the beginning of the source video; for example, "
    "5 minutes 26.5 seconds must be written as 05:26.500, never as 526.5 or as "
    "elapsed seconds. The minute component must have at least two digits and may "
    "exceed 99 for long videos. Cover the complete supplied interval. Mark unclear "
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
                    "language": {"type": "string"},
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


def config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()


def load_keys():
    """Return (name, key) pairs in order, from the environment or the secrets file."""
    values = {name: os.environ[name] for name in KEY_NAMES if name in os.environ}
    if not values:
        path = config_dir() / "secrets/work-with-youtube.env"
        if not path.exists():
            sys.exit(f"Missing Gemini credentials file: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    keys = [(name, values[name]) for name in KEY_NAMES if values.get(name)]
    if not keys:
        sys.exit("Missing required Gemini credential: GEMINI_API_KEY")
    return keys


def build_request(url, start, end, prompt):
    config = {"maxOutputTokens": MAX_OUTPUT_TOKENS}
    if prompt is None:
        prompt = TRANSCRIPT_PROMPT
        config["responseMimeType"] = "application/json"
        config["responseJsonSchema"] = TRANSCRIPT_RESPONSE_SCHEMA
    video = {
        "fileData": {"fileUri": url, "mimeType": "video/*"},
        "videoMetadata": {"startOffset": f"{start}s", "endOffset": f"{end}s"},
    }
    return {
        "contents": [{"role": "user", "parts": [video, {"text": prompt}]}],
        "generationConfig": config,
    }


def post(key, body):
    """Send one request and return (HTTP status, decoded JSON)."""
    request = urllib.request.Request(
        ENDPOINT,
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


def call_gemini(keys, body, cooldown_file, send=post, sleep=time.sleep, now=time.time):
    """Return the first successful response, moving on from keys that fail or are resting."""
    cooldowns = json.loads(cooldown_file.read_text()) if cooldown_file.exists() else {}
    status, message = None, ""
    for name, key in keys:
        if cooldowns.get(name, 0) > now():
            continue
        for attempt in range(RETRIES + 1):
            status, payload = send(key, body)
            if status == 200:
                return payload
            message = payload.get("error", {}).get("message", "")
            if status in TRANSIENT_STATUSES and attempt < RETRIES:
                sleep(2**attempt)
                continue
            if status == 429:
                cooldowns[name] = now() + retry_delay(payload)
                cooldown_file.parent.mkdir(parents=True, exist_ok=True)
                cooldown_file.write_text(json.dumps(cooldowns))
            bad_key = status == 400 and "api key" in message.lower()
            if status in TRANSIENT_STATUSES or status in KEY_STATUSES or bad_key:
                break
            sys.exit(f"Gemini request failed ({status}): {message}")
    if status is None or status == 429:
        wait = min(until for until in cooldowns.values() if until > now()) - now()
        sys.exit(f"Every Gemini key is cooling down; try again in {wait:.0f} seconds")
    sys.exit(f"Every Gemini key failed; last error ({status}): {message}")


def answer_text(response):
    """Return (text, finishReason) from a Gemini response."""
    candidate = (response.get("candidates") or [{}])[0]
    parts = candidate.get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts)
    return text, candidate.get("finishReason", "UNKNOWN")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video-url", required=True)
    parser.add_argument("--start", required=True, type=int, help="seconds")
    parser.add_argument("--end", required=True, type=int, help="seconds")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--transcript", action="store_true")
    mode.add_argument("--prompt")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 0 <= args.start < args.end:
        sys.exit("Require 0 <= start < end")

    body = build_request(args.video_url, args.start, args.end, args.prompt)
    cooldown_file = config_dir() / "mcp-servers/youtube/state/gemini-cooldowns.json"
    text, finish_reason = answer_text(call_gemini(load_keys(), body, cooldown_file))
    complete = finish_reason == "STOP"
    if args.output is None:
        print(text)
    else:
        # An incomplete answer is kept, but never under a reusable name.
        output = args.output if complete else Path(f"{args.output}.partial")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
        print(output)
    if not complete:
        sys.exit(f"Incomplete answer (finishReason {finish_reason}); use a smaller clip")


if __name__ == "__main__":
    main()
