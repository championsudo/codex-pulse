"""CLI and launcher regressions using only empty temporary data directories."""
from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

import server

ROOT = Path(__file__).resolve().parents[1]


class PortabilityTests(unittest.TestCase):
    def test_missing_timezone_explains_installation_and_does_not_bind(self):
        error = io.StringIO()
        with tempfile.TemporaryDirectory(prefix="empty codex home ") as home:
            with patch.object(sys, "argv", ["server.py", "--codex-home", home]), \
                    patch.object(server, "UsageIndex", side_effect=ZoneInfoNotFoundError("synthetic")), \
                    patch.object(server, "Server") as server_type, \
                    contextlib.redirect_stderr(error):
                with self.assertRaises(SystemExit) as raised:
                    server.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("pip install tzdata", error.getvalue())
        self.assertIn("--timezone", error.getvalue())
        server_type.assert_not_called()

    def test_cli_forwards_explicit_home_timezone_and_port(self):
        with tempfile.TemporaryDirectory(prefix="empty codex home ") as home:
            with patch.object(sys, "argv", ["server.py", "--codex-home", home,
                                          "--timezone", "UTC", "--port", "18765"]), \
                    patch.object(server, "UsageIndex") as index_type, \
                    patch.object(server, "Server") as server_type, \
                    patch("builtins.print"):
                server_type.return_value.serve_forever.side_effect = KeyboardInterrupt
                self.assertEqual(server.main(), 0)
                index_type.assert_called_once_with(home, "UTC", paseo_home=None, claude_home=None, opencode_home=None, opencode_db=None)
                server_type.assert_called_once_with(18765, index_type.return_value)
                server_type.return_value.server_close.assert_called_once_with()

    def test_codex_home_environment_is_used_when_no_explicit_path(self):
        with tempfile.TemporaryDirectory(prefix="empty codex home ") as home:
            with patch.dict(os.environ, {"CODEX_HOME": home}), \
                    patch.object(sys, "argv", ["server.py", "--timezone", "UTC"]), \
                    patch.object(server, "UsageIndex") as index_type, \
                    patch.object(server, "Server") as server_type, \
                    patch("builtins.print"):
                server_type.return_value.serve_forever.side_effect = KeyboardInterrupt
                self.assertEqual(server.main(), 0)
                index_type.assert_called_once_with(home, "UTC", paseo_home=None, claude_home=None, opencode_home=None, opencode_db=None)

    def test_invalid_port_fails_before_reading_any_home(self):
        with patch.object(sys, "argv", ["server.py", "--port", "80"]), \
                patch.object(server, "UsageIndex") as index_type, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                server.main()
        self.assertEqual(raised.exception.code, 2)
        index_type.assert_not_called()

    def test_help_works_when_redirected_output_uses_a_legacy_encoding(self):
        result = subprocess.run([sys.executable, str(ROOT / "server.py"), "--help"],
                                capture_output=True, timeout=20,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                                     "PYTHONIOENCODING": "cp1252"})
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertIn(b"--codex-home", result.stdout)
        self.assertNotIn(b"UnicodeEncodeError", result.stderr)

    def test_launcher_runs_from_another_directory_with_spaces_in_project_path(self):
        with tempfile.TemporaryDirectory(prefix="codex pulse project ") as temporary:
            project = Path(temporary) / "project with spaces"
            project.mkdir()
            for name in ("server.py", "usage.py", "providers.py", "start.sh", "start.cmd"):
                shutil.copyfile(ROOT / name, project / name)
            if os.name == "nt":
                command = [str(project / "start.cmd"), "--help"]
            else:
                if not shutil.which("bash"):
                    self.skipTest("bash is not installed")
                command = ["bash", str(project / "start.sh"), "--help"]
            result = subprocess.run(command, cwd=temporary, capture_output=True,
                                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                    timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertIn(b"--codex-home", result.stdout)
        self.assertIn(b"--timezone", result.stdout)
        self.assertIn(b"--port", result.stdout)
