import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import gemini_video


OK = (200, {"candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}]})


KEYS = [("GEMINI_API_KEY", "first"), ("GEMINI_API_KEY_FALLBACK", "second")]
RATE_LIMITED = (429, {"error": {"message": "quota"}})


class CallGeminiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cooldown_file = Path(directory.name) / "state" / "cooldowns.json"
        self.time = 1000.0

    def call(self, replies, keys=KEYS):
        """Play back replies and return the keys that were used."""
        remaining = list(replies)
        keys_used = []

        def send(key, body):
            keys_used.append(key)
            return remaining.pop(0)

        self.keys_used = keys_used
        gemini_video.call_gemini(
            keys, {}, self.cooldown_file, send=send, sleep=lambda _: None, now=lambda: self.time
        )
        return keys_used

    def test_first_key_success_never_touches_the_second(self):
        self.assertEqual(self.call([OK]), ["first"])

    def test_rate_limit_moves_to_the_second_key(self):
        self.assertEqual(self.call([RATE_LIMITED, OK]), ["first", "second"])

    def test_invalid_key_moves_to_the_second_key(self):
        reply = (400, {"error": {"message": "API key not valid"}})
        self.assertEqual(self.call([reply, OK]), ["first", "second"])

    def test_transient_failure_is_retried_on_the_same_key(self):
        self.assertEqual(self.call([(503, {}), (0, {}), OK]), ["first"] * 3)

    def test_exhausted_retries_move_to_the_second_key(self):
        self.assertEqual(self.call([(503, {})] * 3 + [OK]), ["first"] * 3 + ["second"])

    def test_bad_request_stops_without_trying_the_second_key(self):
        with self.assertRaisesRegex(SystemExit, "bad clip"):
            self.call([(400, {"error": {"message": "bad clip"}})])
        self.assertEqual(self.keys_used, ["first"])

    def test_rate_limited_key_rests_during_the_next_call(self):
        self.call([RATE_LIMITED, OK])
        self.assertEqual(self.call([OK]), ["second"])

    def test_rested_key_is_used_again_after_its_cooldown(self):
        self.call([RATE_LIMITED, OK])
        self.time += gemini_video.COOLDOWN_SECONDS + 1
        self.assertEqual(self.call([OK]), ["first"])

    def test_cooldown_follows_the_delay_gemini_requests(self):
        reply = (429, {"error": {"details": [{"retryDelay": "300s"}]}})
        self.call([reply, OK])
        self.time += 299
        self.assertEqual(self.call([OK]), ["second"])
        self.time += 2
        self.assertEqual(self.call([OK]), ["first"])

    def test_every_key_resting_reports_the_wait_without_sending(self):
        with self.assertRaisesRegex(SystemExit, "try again in 60 seconds"):
            self.call([RATE_LIMITED, RATE_LIMITED])
        with self.assertRaisesRegex(SystemExit, "cooling down"):
            self.call([])
        self.assertEqual(self.keys_used, [])

    def test_keys_are_never_written_to_the_cooldown_file(self):
        self.call([RATE_LIMITED, OK])
        self.assertNotIn("first", self.cooldown_file.read_text(encoding="utf-8"))


class LoadKeysTests(unittest.TestCase):
    def load(self, environment):
        with mock.patch.dict(os.environ, environment, clear=True):
            return gemini_video.load_keys()

    def test_reads_both_keys_from_the_secrets_file_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            secrets = Path(directory) / "secrets"
            secrets.mkdir()
            (secrets / "work-with-youtube.env").write_text(
                "GEMINI_API_KEY_FALLBACK=second\nGEMINI_API_KEY=first\n", encoding="utf-8"
            )
            keys = self.load({"CLAUDE_CONFIG_DIR": directory})
        self.assertEqual([key for _, key in keys], ["first", "second"])

    def test_environment_overrides_the_file(self):
        self.assertEqual(self.load({"GEMINI_API_KEY": "from-env"}), [("GEMINI_API_KEY", "from-env")])

    def test_missing_file_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(SystemExit, "Missing Gemini credentials"):
                self.load({"CLAUDE_CONFIG_DIR": directory})


class MainTests(unittest.TestCase):
    def run_main(self, response, output):
        arguments = ["gemini_video.py", "--video-url", "https://youtu.be/x", "--start", "0",
                     "--end", "60", "--transcript", "--output", str(output)]
        with mock.patch.object(sys, "argv", arguments), \
                mock.patch.object(gemini_video, "load_keys", return_value=KEYS), \
                mock.patch.object(gemini_video, "call_gemini", return_value=response):
            gemini_video.main()

    def test_complete_answer_is_saved_under_the_requested_name(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "video" / "transcript-0-60.json"
            with mock.patch("builtins.print"):
                self.run_main(OK[1], output)
            self.assertEqual(output.read_text(encoding="utf-8"), "hi\n")

    def test_incomplete_answer_is_saved_only_as_partial(self):
        response = json.loads(json.dumps(OK[1]))
        response["candidates"][0]["finishReason"] = "MAX_TOKENS"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "transcript-0-60.json"
            with mock.patch("builtins.print"), self.assertRaisesRegex(SystemExit, "MAX_TOKENS"):
                self.run_main(response, output)
            self.assertFalse(output.exists())
            self.assertTrue(Path(f"{output}.partial").exists())


class BuildRequestTests(unittest.TestCase):
    def test_transcript_mode_clips_the_video_and_requires_json(self):
        request = gemini_video.build_request("https://youtu.be/x", 10, 70, None)
        video, prompt = request["contents"][0]["parts"]
        self.assertEqual(video["videoMetadata"], {"startOffset": "10s", "endOffset": "70s"})
        self.assertIn("TRANSCRIPT-ONLY", prompt["text"])
        self.assertEqual(request["generationConfig"]["responseMimeType"], "application/json")

    def test_prompt_mode_sends_the_prompt_as_plain_text(self):
        request = gemini_video.build_request("https://youtu.be/x", 0, 60, "What is on the board?")
        self.assertEqual(request["contents"][0]["parts"][1]["text"], "What is on the board?")
        self.assertNotIn("responseMimeType", request["generationConfig"])


if __name__ == "__main__":
    unittest.main()
