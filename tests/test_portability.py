import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from jobbot.applications import load_profile
from jobbot.status import summary


ROOT = Path(__file__).resolve().parents[1]


class PortabilityTests(unittest.TestCase):
    def test_missing_status_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing.sqlite'
            with self.assertRaises(sqlite3.OperationalError):
                summary(path)
            self.assertFalse(path.exists())

    def test_fictional_profile_cannot_be_used_for_real_letters(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = json.loads((ROOT / 'examples/profile.example.json').read_text(encoding='utf-8'))
            (Path(directory) / 'profile.json').write_text(json.dumps(profile), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'fictional'):
                load_profile(directory)

    def test_offline_demo_is_utf8_and_idempotent_even_with_legacy_console_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'demo.sqlite'
            env = dict(os.environ, PYTHONIOENCODING='cp1251')
            outputs = []
            for name in ('baseline', 'new', 'new'):
                result = subprocess.run(
                    [sys.executable, '-m', 'jobbot', '--config', 'examples/demo.toml',
                     '--snapshot', f'examples/{name}.json', '--database', str(database)],
                    cwd=ROOT, env=env, capture_output=True, check=True,
                )
                outputs.append(result.stdout.decode('utf-8'))
            self.assertNotIn('💼', outputs[0])
            self.assertIn('💼', outputs[1])
            self.assertIn('НЕ ОТПРАВЛЕНО', outputs[1])
            status = summary(database)
            self.assertEqual(status['integrity'], 'ok')
            self.assertEqual(status['mode']['mode'], 'dry-run')
            self.assertEqual(status['outbox'], {'pending': 1})


if __name__ == '__main__':
    unittest.main()
