from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import connection
import connection_settings as settings


class ManagedConfirmation(unittest.TestCase):
    def test_confirmation_preserves_session_and_watch_off(self):
        with tempfile.TemporaryDirectory() as directory, closing(connection(Path(directory)/'state')) as db:
            config = dict(mode='managed', launch_nonce='nonce', manual_exclusion_acknowledged=False)
            db.execute('INSERT INTO connection_settings VALUES (1,?)', (json.dumps(config),))
            with patch('desktop_binding.live_binding', return_value=True):
                settings.acknowledge_managed(db)
            saved = json.loads(db.execute('SELECT value FROM connection_settings').fetchone()[0])
            self.assertTrue(saved['manual_exclusion_acknowledged'])
            self.assertEqual(saved['launch_nonce'], 'nonce')

    def test_unverified_connection_cannot_be_confirmed(self):
        with tempfile.TemporaryDirectory() as directory, closing(connection(Path(directory)/'state')) as db:
            config = dict(mode='managed', manual_exclusion_acknowledged=False)
            db.execute('INSERT INTO connection_settings VALUES (1,?)', (json.dumps(config),))
            with patch('desktop_binding.live_binding', return_value=False):
                with self.assertRaises(ValueError):
                    settings.acknowledge_managed(db)
            self.assertEqual(json.loads(db.execute('SELECT value FROM connection_settings').fetchone()[0]), config)
