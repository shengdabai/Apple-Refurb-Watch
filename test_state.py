import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import monitor

class StateTest(unittest.TestCase):
    def test_only_missing_state_initializes_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with patch.object(monitor, "STATE_FILE", path):
                self.assertFalse(monitor.load_state()["first_run_done"])
                for content in ["{broken", "[]", '{"seen":{},"first_run_done":"false"}']:
                    path.write_text(content)
                    with self.assertRaises(RuntimeError):
                        monitor.load_state()
                    self.assertEqual(path.read_text(), content)
                state = {"first_run_done": True, "seen": {"fixture": {}}}
                path.write_text(json.dumps(state))
                self.assertEqual(monitor.load_state(), state)

    def test_unreadable_state_does_not_reset(self):
        with patch.object(Path, "read_text", side_effect=PermissionError("fixture")):
            with self.assertRaises(RuntimeError):
                monitor.load_state()

if __name__ == "__main__":
    unittest.main()
