"""Offline checks for the ingestion command; no database or API calls."""
import subprocess
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "app/mult_agents/rag/ingest.py"


class IngestCliTests(unittest.TestCase):
    def test_help_is_available_without_starting_services(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("input_path", result.stdout)

    def test_missing_input_is_reported_before_database_initialization(self):
        missing = SCRIPT.parent / "__missing_environment_probe__.md"
        result = subprocess.run([sys.executable, str(SCRIPT), str(missing)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Input path does not exist", result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)


if __name__ == "__main__":
    unittest.main()
