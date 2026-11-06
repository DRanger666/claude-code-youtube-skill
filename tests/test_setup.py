import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SETUP = Path(__file__).resolve().parents[1] / "scripts" / "setup_youtube_mcp.sh"
FAKE_CLAUDE = """#!/bin/sh
printf '%s\\n' "$*" >>"$CALLS"
if [ "$2" = list ] && [ -f "$REGISTERED" ]; then echo 'youtube: node server - Connected'; fi
if [ "$2" = add ]; then : >"$REGISTERED"; fi
"""


class SetupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.calls = self.root / "calls"
        server = self.root / "mcp-servers/youtube/app/dist/stdio-server.js"
        server.parent.mkdir(parents=True)
        server.touch()
        fake = self.root / "bin/claude"
        fake.parent.mkdir()
        fake.write_text(FAKE_CLAUDE, encoding="utf-8")
        fake.chmod(0o755)
        self.environment = {
            **os.environ,
            "PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}",
            "CLAUDE_CONFIG_DIR": str(self.root),
            "CALLS": str(self.calls),
            "REGISTERED": str(self.root / "registered"),
        }

    def run_setup(self):
        return subprocess.run(
            ["sh", str(SETUP)], env=self.environment, capture_output=True, text=True, check=True
        )

    def test_registers_at_user_scope_once(self):
        self.run_setup()
        second = self.run_setup()
        added = [line for line in self.calls.read_text().splitlines() if " add " in f" {line} "]
        self.assertEqual(len(added), 1)
        self.assertTrue(added[0].startswith("mcp add --scope user youtube -- "))
        self.assertIn("left unchanged", second.stdout)

    def test_never_calls_mcp_get_which_prints_the_api_key(self):
        self.run_setup()
        self.run_setup()
        self.assertNotIn("mcp get", self.calls.read_text())


if __name__ == "__main__":
    unittest.main()
