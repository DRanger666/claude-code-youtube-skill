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


KEYS = [gemini_video.Key("GEMINI_API_KEY", "first", "paid"), gemini_video.Key("GEMINI_API_KEY_FALLBACK", "second", None)]
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

    def load_file(self, text):
        with tempfile.TemporaryDirectory() as directory:
            secrets = Path(directory) / "secrets"
            secrets.mkdir()
            (secrets / "work-with-youtube.env").write_text(text, encoding="utf-8")
            return self.load({"CLAUDE_CONFIG_DIR": directory})

    def test_reads_both_keys_from_the_secrets_file_in_order(self):
        keys = self.load_file("GEMINI_API_KEY_FALLBACK=second\nGEMINI_API_KEY=first\n")
        self.assertEqual([key.value for key in keys], ["first", "second"])

    def test_one_key_is_enough(self):
        self.assertEqual(self.load_file("GEMINI_API_KEY=only\n"), [("GEMINI_API_KEY", "only", None)])

    def test_labels_say_where_keys_came_from(self):
        keys = self.load_file("GEMINI_API_KEY=first\nGEMINI_API_KEY_LABEL=paid, project yt\nGEMINI_API_KEY_FALLBACK=second\n")
        self.assertEqual([key.label for key in keys], ["paid, project yt", None])

    def test_environment_overrides_the_file(self):
        keys = self.load({"GEMINI_API_KEY": "from-env", "GEMINI_API_KEY_LABEL": "test"})
        self.assertEqual(keys, [("GEMINI_API_KEY", "from-env", "test")])

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

    def run_main(self, call_gemini, output=None, extra=(), mode=("--transcript",), start="0"):
        """Run the script, with the audio download and cut stood in for; keep
        its stderr lines and its record."""
        arguments = ["gemini_video.py", "--video-url", "https://youtu.be/DStYkSO4Cc0?t=30", "--start", start,
                     "--end", "60", *mode, *extra]
        if output:
            arguments += ["--output", str(output)]
        logged = []

        def download_audio(video, audio):
            audio["downloaded"] = False
            return Path(f"/cache/{video}.webm")
        with mock.patch.object(sys, "argv", arguments), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.directory)}), \
                mock.patch.object(gemini_video, "load_keys", return_value=KEYS), \
                mock.patch.object(gemini_video, "download_audio", side_effect=download_audio), \
                mock.patch.object(gemini_video, "cut_audio", return_value=b"mp3 clip"), \
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
            "video": "https://www.youtube.com/watch?v=DStYkSO4Cc0", "start": 0, "end": 60,
            "mode": "transcript", "prompt": None,
            "model": gemini_video.MODEL, "thinking_level": gemini_video.THINKING_LEVEL,
            "max_output_tokens": None, "caller": "claude-code", "note": None,
            "keys": {
                "GEMINI_API_KEY": {"label": "paid", "fingerprint": gemini_video.fingerprint("first")},
                "GEMINI_API_KEY_FALLBACK": {"label": None, "fingerprint": gemini_video.fingerprint("second")},
            },
            "audio": {"downloaded": False, "source": "/cache/DStYkSO4Cc0.webm", "clip_bytes": 8,
                      "timestamps_from": "clip"},
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
            "gemini_video.load_keys = lambda: [gemini_video.Key('GEMINI_API_KEY', 'k', None)]\n"
            "gemini_video.download_audio = lambda video, audio: 'cached.webm'\n"
            "gemini_video.cut_audio = lambda source, start, end: b'clip'\n"
            "gemini_video.post = lambda key, body, model: (print('sent', flush=True), time.sleep(30))\n"
            "sys.argv = ['gemini_video.py', '--video-url', 'DStYkSO4Cc0', '--start', '0', '--end', '60', '--transcript']\n"
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
        self.assertNotIn("first", json.dumps(self.record))
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


    def test_a_transcript_sends_the_clip_as_audio_and_gives_video_timestamps(self):
        sent = {}
        transcript = {
            "clip_start_timestamp": "00:00.000", "clip_end_timestamp": "00:30.000",
            "segments": [{"start_timestamp": "00:01.250", "end_timestamp": "00:04.000", "vocal_type": "spoken",
                          "language": "English", "text": "hello", "audibility": "clear"}],
            "completed_through_timestamp": "00:30.000", "transcription_complete": True, "truncation_detected": False,
        }
        response = json.loads(json.dumps(ANSWER))
        response["candidates"][0]["content"]["parts"] = [{"text": json.dumps(transcript)}]

        def call_gemini(keys, body, cooldown_file, attempts, **_):
            sent["parts"] = body["contents"][0]["parts"]
            return response
        output = self.directory / "transcript.json"
        self.run_main(call_gemini, output, start="30")
        self.assertEqual(sent["parts"][0], {"inlineData": {"mimeType": "audio/mp3", "data": "bXAzIGNsaXA="}})
        saved = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual((saved["clip_start_timestamp"], saved["clip_end_timestamp"]), ("00:30.000", "01:00.000"))
        self.assertEqual(saved["segments"][0]["start_timestamp"], "00:31.250")
        self.assertEqual(self.record["audio"]["timestamps_from"], "video")

    def test_a_question_sends_the_link_without_its_time_and_the_clip_range(self):
        sent = {}

        def call_gemini(keys, body, cooldown_file, attempts, **_):
            sent["parts"] = body["contents"][0]["parts"]
            return ANSWER
        self.run_main(call_gemini, mode=("--prompt", "What is on the board?"))
        self.assertEqual(sent["parts"][0], {
            "fileData": {"fileUri": "https://www.youtube.com/watch?v=DStYkSO4Cc0", "mimeType": "video/*"},
            "videoMetadata": {"startOffset": "0s", "endOffset": "60s"},
        })
        self.assertNotIn("audio", self.record)


class BuildRequestTests(unittest.TestCase):
    def test_a_transcript_requires_json(self):
        request = gemini_video.build_request({"inlineData": {}}, None)
        media, prompt = request["contents"][0]["parts"]
        self.assertEqual(media, {"inlineData": {}})
        self.assertIn("TRANSCRIPT-ONLY", prompt["text"])
        self.assertEqual(request["generationConfig"]["responseMimeType"], "application/json")

    def test_unset_settings_are_left_to_the_model(self):
        request = gemini_video.build_request({}, "What is on the board?")
        self.assertEqual(request["generationConfig"], {"thinkingConfig": {"includeThoughts": True}})

    def test_prompt_mode_sends_the_prompt_as_plain_text(self):
        request = gemini_video.build_request({}, "What is on the board?")
        self.assertEqual(request["contents"][0]["parts"][1]["text"], "What is on the board?")
        self.assertNotIn("responseMimeType", request["generationConfig"])


class VideoIdTests(unittest.TestCase):
    def test_usual_links_and_a_bare_id_give_the_id_without_times(self):
        for link in [
            "DStYkSO4Cc0", " DStYkSO4Cc0 ",
            "https://www.youtube.com/watch?v=DStYkSO4Cc0&t=60s&list=PL1",
            "youtube.com/watch?v=DStYkSO4Cc0", "https://m.youtube.com/watch?v=DStYkSO4Cc0",
            "https://youtu.be/DStYkSO4Cc0?t=60", "https://www.youtube.com/shorts/DStYkSO4Cc0",
            "https://www.youtube.com/live/DStYkSO4Cc0", "https://www.youtube.com/embed/DStYkSO4Cc0?start=30&end=60",
        ]:
            self.assertEqual(gemini_video.video_id(link), "DStYkSO4Cc0", link)

    def test_other_links_are_refused(self):
        for link in ["https://example.com/watch?v=DStYkSO4Cc0", "https://www.youtube.com/watch?v=short",
                     "https://www.youtube.com/@channel", ""]:
            self.assertIsNone(gemini_video.video_id(link), link)


class ShiftTimestampsTests(unittest.TestCase):
    def test_timestamps_move_by_the_clip_start(self):
        text = json.dumps({"clip_start_timestamp": "00:00.000", "clip_end_timestamp": "10:00.000",
                           "completed_through_timestamp": "09:59.999",
                           "segments": [{"start_timestamp": "00:59.500", "end_timestamp": "01:00.250"}]})
        shifted = json.loads(gemini_video.shift_timestamps(text, 5970))
        self.assertEqual((shifted["clip_start_timestamp"], shifted["clip_end_timestamp"]), ("99:30.000", "109:30.000"))
        self.assertEqual(shifted["completed_through_timestamp"], "109:29.999")
        self.assertEqual(shifted["segments"][0], {"start_timestamp": "100:29.500", "end_timestamp": "100:30.250"})

    def test_an_answer_that_is_not_a_transcript_is_left_alone(self):
        self.assertIsNone(gemini_video.shift_timestamps('{"segments": [', 30))
        self.assertIsNone(gemini_video.shift_timestamps('{"segments": []}', 30))


def finished(returncode=0, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class DownloadAudioTests(unittest.TestCase):
    """yt-dlp, uv and the network stood in for by a fake run()."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache = Path(directory.name) / "work-with-youtube/audio"
        self.enterContext(mock.patch.dict(os.environ, {"XDG_CACHE_HOME": directory.name}))
        self.enterContext(mock.patch.object(gemini_video.shutil, "which", lambda name: f"/bin/{name}"))
        self.commands = []

    def fake_run(self, downloads, versions=("2026.10.26",)):
        """downloads: per download attempt, True to succeed or an error line."""
        downloads, versions = list(downloads), list(versions)

        def run(command):
            self.commands.append(command[1] if command[0] != "/bin/uv" else "upgrade")
            if command[1] == "--version":
                return finished(stdout=(versions.pop(0) if len(versions) > 1 else versions[0]).encode() + b"\n")
            if command[0] == "/bin/uv":
                return finished()
            outcome = downloads.pop(0)
            if outcome is True:
                Path(command[command.index("-o") + 1].replace("%(ext)s", "webm")).write_bytes(b"audio")
                return finished()
            return finished(1, stderr=f"WARNING: noise\nERROR: {outcome}\n".encode())
        return run

    def download(self, run):
        self.audio = {}
        return gemini_video.download_audio("DStYkSO4Cc0", self.audio, run=run)

    def test_the_first_download_is_cached_whole(self):
        path = self.download(self.fake_run([True]))
        self.assertEqual(path, self.cache / "DStYkSO4Cc0.webm")
        self.assertEqual(path.read_bytes(), b"audio")
        self.assertEqual(sorted(p.name for p in self.cache.iterdir()), ["DStYkSO4Cc0.webm"])
        self.assertEqual(self.audio, {"downloaded": True, "yt_dlp": "2026.10.26"})

    def test_a_cached_track_is_used_without_running_anything(self):
        self.cache.mkdir(parents=True)
        (self.cache / "DStYkSO4Cc0.m4a").write_bytes(b"audio")
        self.assertEqual(self.download(self.fake_run([])), self.cache / "DStYkSO4Cc0.m4a")
        self.assertEqual((self.commands, self.audio), ([], {"downloaded": False}))

    def test_a_failed_download_upgrades_yt_dlp_and_tries_again(self):
        path = self.download(self.fake_run(["Sign in to confirm", True], versions=["2026.10.26", "2026.12.03"]))
        self.assertTrue(path.exists())
        self.assertEqual(self.commands, ["--version", "--quiet", "upgrade", "--version", "--quiet"])
        self.assertEqual(self.audio["upgraded"], {"from": "2026.10.26", "to": "2026.12.03"})
        self.assertEqual(self.audio["yt_dlp"], "2026.12.03")

    def test_when_no_newer_yt_dlp_exists_the_error_is_reported(self):
        with self.assertRaisesRegex(gemini_video.GeminiError,
                                    r"^yt-dlp 2026\.10\.26 could not download the audio: ERROR: Video unavailable$"):
            self.download(self.fake_run(["Video unavailable"]))
        self.assertEqual(self.commands, ["--version", "--quiet", "upgrade", "--version"])
        self.assertEqual([p.name for p in self.cache.iterdir()], [])

    def test_a_failure_after_upgrading_names_the_new_version(self):
        with self.assertRaisesRegex(gemini_video.GeminiError, r"^yt-dlp 2026\.12\.03 could not download"):
            self.download(self.fake_run(["Private video", "Private video"], versions=["2026.10.26", "2026.12.03"]))

    def test_a_missing_yt_dlp_says_how_to_install_it(self):
        with mock.patch.object(gemini_video.shutil, "which", lambda name: None), \
                self.assertRaisesRegex(gemini_video.GeminiError, r"yt-dlp is not installed; install it with: uv tool"):
            self.download(self.fake_run([]))


class CutAudioTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(gemini_video.shutil, "which", lambda name: f"/bin/{name}"))

    def test_the_clip_is_cut_from_its_start_for_its_length_as_mono_mp3(self):
        commands = []
        clip = gemini_video.cut_audio(Path("a.webm"), 3000, 3600,
                                      run=lambda command: commands.append(command) or finished(stdout=b"mp3"))
        self.assertEqual(clip, b"mp3")
        command = " ".join(commands[0])
        self.assertIn("-ss 3000 -t 600 -i a.webm", command)
        self.assertIn(f"-ac 1 -b:a {gemini_video.AUDIO_BITRATE} -f mp3 pipe:1", command)

    def test_a_clip_past_the_end_of_the_video_is_reported(self):
        with self.assertRaisesRegex(gemini_video.GeminiError, "past the end of the video"):
            gemini_video.cut_audio(Path("a.webm"), 9000, 9060, run=lambda command: finished())


if __name__ == "__main__":
    unittest.main()
