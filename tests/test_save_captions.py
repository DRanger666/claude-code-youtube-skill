import json
import shutil
import subprocess
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "save_captions.mjs"
CALL = """
const script = await import(process.argv[2]);
const [name, ...args] = JSON.parse(process.argv[1]);
try { console.log(JSON.stringify(script[name](...args))); }
catch (error) { console.log(JSON.stringify({ error: error.message })); }
"""


def call(*arguments):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", CALL, json.dumps(arguments), str(SCRIPT)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def tracks(*codes, kind="asr"):
    return [{"language_code": code, "kind": kind} for code in codes]


def original_audio(language):
    return [{"language": "ar", "is_original": False}, {"language": language, "is_original": True}]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SpokenLanguageTrackTests(unittest.TestCase):
    def test_dubbed_video_uses_the_original_audio_language_not_the_first_track(self):
        result = call("spokenLanguageTrack", tracks("ar", "bn", "en"), original_audio("en-US"))
        self.assertEqual(result["language_code"], "en")

    def test_uploaded_captions_are_preferred_over_auto_generated_ones(self):
        available = tracks("en") + [{"language_code": "en-GB"}]
        result = call("spokenLanguageTrack", available, original_audio("en"))
        self.assertEqual(result["language_code"], "en-GB")

    def test_captions_in_other_languages_are_refused_rather_than_translated(self):
        result = call("spokenLanguageTrack", tracks("ar", "en"), original_audio("es"))
        self.assertEqual(result["error"], "No captions in the spoken language es; captions: ar, en")

    def test_a_single_track_needs_no_audio_language(self):
        result = call("spokenLanguageTrack", tracks("es"), [{"is_original": True}])
        self.assertEqual(result["language_code"], "es")

    def test_several_tracks_with_no_audio_language_are_refused(self):
        result = call("spokenLanguageTrack", tracks("en", "es"), [{"is_original": True}])
        self.assertEqual(
            result["error"], "YouTube does not report the spoken language; captions: en, es"
        )

    def test_a_video_without_captions_is_refused(self):
        self.assertEqual(
            call("spokenLanguageTrack", [], original_audio("en"))["error"],
            "This video has no captions",
        )


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class FormatTranscriptTests(unittest.TestCase):
    def test_keeps_every_caption_in_timestamped_paragraphs(self):
        events = [
            {"tStartMs": 1000, "segs": [{"utf8": "[Music]"}]},
            {"tStartMs": 2000, "segs": [{"utf8": "\n"}]},
            {"tStartMs": 61000, "segs": [{"utf8": "first "}, {"utf8": "line\n"}]},
            {"tStartMs": 62000, "segs": [{"utf8": "second line"}]},
            {"segs": [{"utf8": "no timestamp"}]},
        ]
        text = call("formatTranscript", "Title", "abc", "English (auto-generated)", events)
        url = "https://www.youtube.com/watch?v=abc"
        self.assertEqual(
            text,
            f"# Title\n\n- Video: {url}\n- Captions: English (auto-generated), unedited\n\n"
            f"[00:01]({url}&t=1s) [Music] first line\n\n"
            f"[01:02]({url}&t=62s) second line\n",
        )


if __name__ == "__main__":
    unittest.main()
