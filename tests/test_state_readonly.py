"""接続証明の参照は送信側の排他更新を妨げず、状態を作らない。"""
from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest

import core


class ReadonlyState(unittest.TestCase):
    def test_absent_state_is_not_created(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / 'absent'
            with self.assertRaises((OSError, RuntimeError)):
                core.connection(home, readonly=True)
            self.assertFalse(home.exists())

    def test_read_does_not_take_writer_lock_or_allow_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / 'state'
            with closing(core.connection(home)) as writer:
                writer.execute('BEGIN IMMEDIATE')
                with closing(core.connection(home, readonly=True)) as reader:
                    self.assertEqual(reader.execute('SELECT count(*) FROM desktop_sessions').fetchone()[0], 0)
                    self.assertEqual(reader.total_changes, 0)
                    with self.assertRaises(sqlite3.OperationalError):
                        reader.execute('INSERT INTO desktop_sessions VALUES (1,?)', ('{}',))
                    self.assertTrue(writer.in_transaction)
                writer.execute('ROLLBACK')


if __name__ == '__main__':
    unittest.main()
