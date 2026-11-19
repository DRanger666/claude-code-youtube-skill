import json
import os
import signal
import subprocess
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
        self.waits = []
        self.logged = []

        def send(key, body):
            keys_used.append(key)
            return remaining.pop(0)

        self.keys_used = keys_used
        self.attempts = []
        gemini_video.call_gemini(
            keys, {}, self.cooldown_file, self.attempts, send=send, sleep=self.waits.append,
            now=lambda: self.time, log=self.logged.append,
        )
        return keys_used

    def test_each_attempt_is_recorded_and_logged(self):
        self.call([RATE_LIMITED, (503, {"error": {"message": "high demand"}}), OK])
        self.assertEqual(self.attempts, [
            {"key": "GEMINI_API_KEY", "status": 429, "seconds": 0.0, "error": "quota"},
            {"key": "GEMINI_API_KEY_FALLBACK", "status": 503, "seconds": 0.0, "error": "high demand"},
            {"key": "GEMINI_API_KEY_FALLBACK", "status": 200, "seconds": 0.0},
        ])
        self.assertEqual(self.logged, [
            "attempt 1 with GEMINI_API_KEY: 429 in 0.0s: quota",
            "attempt 2 with GEMINI_API_KEY_FALLBACK: 503 in 0.0s: high demand",
            "attempt 3 with GEMINI_API_KEY_FALLBACK: 200 in 0.0s",
        ])
        self.call([OK])
        self.assertRegex(self.logged[0], r"^GEMINI_API_KEY skipped: resting for \d+ more seconds$")
        for line in self.logged:  # the key values are "first" and "second"
            self.assertNotRegex(line, r"\b(first|second)\b")

    def test_first_key_success_never_touches_the_second(self):
        self.assertEqual(self.call([OK]), ["first"])

    def test_rate_limit_moves_to_the_second_key(self):
        self.assertEqual(self.call([RATE_LIMITED, OK]), ["first", "second"])

    def test_invalid_key_moves_to_the_second_key(self):
        reply = (400, {"error": {"message": "API key not valid"}})
        self.assertEqual(self.call([reply, OK]), ["first", "second"])

    def test_transient_failure_is_retried_on_the_same_key(self):
        self.assertEqual(self.call([(503, {}), (0, {}), OK]), ["first"] * 3)

    def test_overload_waits_longer_each_time_with_jitter(self):
        self.assertEqual(self.call([(503, {})] * 4 + [OK]), ["first"] * 5)
        for wait, base in zip(self.waits, [5, 10, 20, 40]):
            self.assertTrue(base <= wait <= base + 1, self.waits)

    def test_exhausted_retries_stop_without_trying_the_second_key(self):
        overloaded = (503, {"error": {"message": "high demand"}})
        with self.assertRaisesRegex(gemini_video.GeminiError, r"^Gemini overloaded or unreachable \(503\) after 5 attempts: high demand; try again in a few minutes$"):
            self.call([overloaded] * 5)
        self.assertEqual(self.keys_used, ["first"] * 5)
        self.assertFalse(self.cooldown_file.exists())

    def test_bad_request_stops_without_trying_the_second_key(self):
        with self.assertRaisesRegex(gemini_video.GeminiError, "bad clip"):
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
        with self.assertRaisesRegex(gemini_video.GeminiError, "try again in 60 seconds"):
            self.call([RATE_LIMITED, RATE_LIMITED])
        with self.assertRaisesRegex(gemini_video.GeminiError, "cooling down"):
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
            with self.assertRaisesRegex(gemini_video.GeminiError, "Missing Gemini credentials"):
                self.load({"CLAUDE_CONFIG_DIR": directory})


ANSWER = {
    "candidates": [{"content": {"parts": [
        {"text": "Listening for speakers. ", "thought": True},
        {"text": "hi"},
        {"text": "Checking the timestamps.", "thought": True},
    ]}, "finishReason": "STOP"}],
    "usageMetadata": {
        "promptTokenCount": 9000, "thoughtsTokenCount": 2500, "candidatesTokenCount": 5692,
        "totalTokenCount": 17192,
        "promptTokensDetails": [{"modality": "VIDEO", "tokenCount": 7000}, {"modality": "AUDIO", "tokenCount": 2000}],
    },
    "modelVersion": "gemini-test",
    "responseId": "abc",
}


class MainTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.records = self.directory / "mcp-servers/youtube/state/gemini-requests.jsonl"

    def run_main(self, call_gemini, output=None, extra=()):
        """Run the script; return its stderr lines and its record."""
        arguments = ["gemini_video.py", "--video-url", "https://youtu.be/x", "--start", "0",
                     "--end", "60", "--transcript", *extra]
        if output:
            arguments += ["--output", str(output)]
        logged = []
        with mock.patch.object(sys, "argv", arguments), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.directory)}), \
                mock.patch.object(gemini_video, "load_keys", return_value=KEYS), \
                mock.patch.object(gemini_video, "call_gemini", side_effect=call_gemini), \
                mock.patch.object(gemini_video, "log_to_stderr", logged.append), \
                mock.patch("builtins.print"):
            try:
                gemini_video.main()
            finally:
                self.logged = logged
                self.record = json.loads(self.records.read_text(encoding="utf-8").splitlines()[-1])

    @staticmethod
    def answering(response):
        def call_gemini(keys, body, cooldown_file, attempts, **_):
            attempts.append({"key": "GEMINI_API_KEY", "status": 200, "seconds": 1.0})
            return response
        return call_gemini

    def test_complete_answer_is_saved_and_recorded(self):
        output = self.directory / "video" / "transcript-0-60.json"
        self.run_main(self.answering(ANSWER), output)
        self.assertEqual(output.read_text(encoding="utf-8"), "hi\n")
        record = self.record
        self.assertRegex(record.pop("id"), r"^\d{8}T\d{6}Z-[0-9a-f]{4}$")
        self.assertIsInstance(record.pop("seconds"), float)
        self.assertEqual(record, {
            "video": "https://youtu.be/x", "start": 0, "end": 60, "mode": "transcript", "prompt": None,
            "model": gemini_video.MODEL, "thinking_level": gemini_video.THINKING_LEVEL,
            "max_output_tokens": None, "caller": "claude-code", "note": None,
            "attempts": [{"key": "GEMINI_API_KEY", "status": 200, "seconds": 1.0}],
            "finish_reason": "STOP", "usage": ANSWER["usageMetadata"],
            "model_version": "gemini-test", "response_id": "abc",
            "thoughts": "Listening for speakers. Checking the timestamps.",
            "output": str(output), "outcome": "ok",
        })
        self.assertRegex(self.logged[-1], r"^request \S+ ok in \d+\.\ds; [^;]+; finish STOP; tokens: "
                         r"input 9000 \(video 7000, audio 2000\), thinking 2500, answer 5692, total 17192$")

    def test_incomplete_answer_is_saved_only_as_partial(self):
        response = json.loads(json.dumps(ANSWER))
        response["candidates"][0]["finishReason"] = "MAX_TOKENS"
        output = self.directory / "transcript-0-60.json"
        with self.assertRaisesRegex(SystemExit, r"MAX_TOKENS\); use a smaller clip, a lower thinking level or a higher output limit$"):
            self.run_main(self.answering(response), output)
        self.assertFalse(output.exists())
        self.assertTrue(Path(f"{output}.partial").exists())
        self.assertEqual((self.record["outcome"], self.record["output"]), ("incomplete", f"{output}.partial"))

    def test_a_failed_request_is_recorded_and_its_error_comes_last(self):
        def overloaded(keys, body, cooldown_file, attempts, **_):
            attempts.append({"key": "GEMINI_API_KEY", "status": 503, "seconds": 2.0, "error": "high demand"})
            raise gemini_video.GeminiError("Gemini overloaded")
        with self.assertRaisesRegex(SystemExit, "^Gemini overloaded$"):
            self.run_main(overloaded)
        self.assertEqual(self.record["outcome"], "failed")
        self.assertEqual(self.record["error"], "Gemini overloaded")
        self.assertEqual(len(self.record["attempts"]), 1)
        self.assertRegex(self.logged[-1], r"^request \S+ failed in \d+\.\ds; [^;]+$")

    def test_a_request_stopped_by_the_caller_is_still_recorded(self):
        waiting = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(Path(gemini_video.__file__).parent)!r})\n"
            "import gemini_video\n"
            "gemini_video.load_keys = lambda: [('GEMINI_API_KEY', 'k')]\n"
            "gemini_video.post = lambda key, body, model: (print('sent', flush=True), time.sleep(30))\n"
            "sys.argv = ['gemini_video.py', '--video-url', 'https://youtu.be/x', '--start', '0', '--end', '60', '--transcript']\n"
            "gemini_video.main()\n"
        )
        child = subprocess.Popen([sys.executable, "-c", waiting], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env={**os.environ, "CLAUDE_CONFIG_DIR": str(self.directory)})
        self.assertEqual(child.stdout.readline(), "sent\n")
        child.send_signal(signal.SIGTERM)
        _, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 1)
        self.assertEqual(stderr.splitlines()[-1], "Stopped by the caller before Gemini answered")
        record = json.loads(self.records.read_text(encoding="utf-8"))
        self.assertEqual((record["outcome"], record["error"]), ("failed", "Stopped by the caller before Gemini answered"))

    def test_the_chosen_settings_are_sent_and_recorded(self):
        sent = {}

        def call_gemini(keys, body, cooldown_file, attempts, send, **_):
            sent["config"] = body["generationConfig"]
            sent["model"] = send.keywords["model"]
            return ANSWER
        self.run_main(call_gemini, extra=["--model", "gemini-test-model", "--thinking-level", "low",
                                          "--max-output-tokens", "30000", "--note", "dense speech", "--caller", "test"])
        self.assertEqual(sent["model"], "gemini-test-model")
        self.assertEqual(sent["config"]["thinkingConfig"], {"includeThoughts": True, "thinkingLevel": "LOW"})
        self.assertEqual(sent["config"]["maxOutputTokens"], 30000)
        self.assertEqual(
            {name: self.record[name] for name in ["model", "thinking_level", "max_output_tokens", "note", "caller"]},
            {"model": "gemini-test-model", "thinking_level": "low", "max_output_tokens": 30000,
             "note": "dense speech", "caller": "test"},
        )
        self.assertIn("; gemini-test-model, thinking low, output limit 30000;", self.logged[-1])

    def test_requests_add_lines_to_one_file(self):
        self.run_main(self.answering(ANSWER))
        self.run_main(self.answering(ANSWER))
        self.assertEqual(len(self.records.read_text(encoding="utf-8").splitlines()), 2)
        self.assertEqual(self.records.stat().st_mode & 0o777, 0o600)


class BuildRequestTests(unittest.TestCase):
    def test_transcript_mode_clips_the_video_and_requires_json(self):
        request = gemini_video.build_request("https://youtu.be/x", 10, 70, None)
        video, prompt = request["contents"][0]["parts"]
        self.assertEqual(video["videoMetadata"], {"startOffset": "10s", "endOffset": "70s"})
        self.assertIn("TRANSCRIPT-ONLY", prompt["text"])
        self.assertEqual(request["generationConfig"]["responseMimeType"], "application/json")

    def test_unset_settings_are_left_to_the_model(self):
        request = gemini_video.build_request("https://youtu.be/x", 0, 60, "What is on the board?")
        self.assertEqual(request["generationConfig"], {"thinkingConfig": {"includeThoughts": True}})

    def test_prompt_mode_sends_the_prompt_as_plain_text(self):
        request = gemini_video.build_request("https://youtu.be/x", 0, 60, "What is on the board?")
        self.assertEqual(request["contents"][0]["parts"][1]["text"], "What is on the board?")
        self.assertNotIn("responseMimeType", request["generationConfig"])


if __name__ == "__main__":
    unittest.main()
