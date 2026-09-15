import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from server import read_snapshot


class ArchetypeClassificationTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.db = root / 'fixture.sqlite3'
        self.groups = root / 'groups.json'
        connection = sqlite3.connect(self.db)
        connection.executescript('''
            CREATE TABLE work_items (
                id TEXT PRIMARY KEY, title TEXT, state TEXT, createdAt INTEGER
            );
            CREATE TABLE sessions (
                sessionKey TEXT PRIMARY KEY, archetype TEXT
            );
            CREATE TABLE assignments (
                id TEXT PRIMARY KEY, workItemId TEXT, holderKey TEXT,
                holderRole TEXT, reviewsAssignmentId TEXT, openedAt INTEGER,
                state TEXT
            );
            CREATE TABLE turns (
                seq INTEGER PRIMARY KEY, assignmentId TEXT, sessionKey TEXT,
                roleRef TEXT, status TEXT, startedAt INTEGER, endedAt INTEGER
            );
            CREATE TABLE attests (
                id TEXT PRIMARY KEY, assignmentId TEXT, ts INTEGER,
                kind TEXT, verdictKind TEXT
            );
        ''')
        connection.executemany(
            'INSERT INTO work_items VALUES (?, ?, ?, ?)',
            [('wi-fixture', 'Fixture item', 'open', 1)],
        )
        connection.executemany(
            'INSERT INTO sessions VALUES (?, ?)',
            [
                ('s-spec', 'spec-writer'),
                ('s-coder', 'coder'),
                ('s-review-spec', 'reviewer-spec'),
                ('s-orchestrator', 'orchestrator'),
                ('s-unknown', 'default'),
            ],
        )
        connection.executemany(
            'INSERT INTO assignments VALUES (?, ?, ?, ?, ?, ?, ?)',
            [
                ('a-spec', 'wi-fixture', 's-spec', None, None, 1, 'closed'),
                ('a-coder', 'wi-fixture', 's-coder', None, None, 2, 'closed'),
                # The holder is a coder, but its turn is performed by an orchestrator.
                ('a-supervised', 'wi-fixture', 's-coder', 'coder', None, 3, 'closed'),
                # Unknown holder archetype plus an explicit review link falls back to Code review.
                ('a-review', 'wi-fixture', 's-unknown', 'reviewer', 'a-coder', 4, 'closed'),
                ('a-spec-review', 'wi-fixture', 's-unknown', None, 'a-spec', 5, 'closed'),
                # A later review is the successor after the adverse review.
                ('a-retry', 'wi-fixture', 's-unknown', 'reviewer', 'a-coder', 9, 'closed'),
                # No archetype, role, or review link remains Unlabelled.
                ('a-unknown', 'wi-fixture', 's-unknown', None, None, 6, 'closed'),
            ],
        )
        connection.executemany(
            'INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)',
            [
                (1, 'a-spec', 's-spec', None, 'delivered', 10, 20),
                (2, 'a-coder', 's-coder', None, 'delivered', 30, 40),
                (3, 'a-supervised', 's-orchestrator', 'coder', 'delivered', 50, 60),
                (4, 'a-review', 's-unknown', None, 'delivered', 70, 80),
                (5, 'a-spec-review', 's-review-spec', None, 'delivered', 90, 100),
                (6, 'a-unknown', 's-unknown', None, 'delivered', 110, 120),
            ],
        )
        connection.execute(
            'INSERT INTO attests VALUES (?, ?, ?, ?, ?)',
            ('v-review', 'a-review', 7, 'verdict', 'changes-requested'),
        )
        connection.commit()
        connection.close()
        self.groups.write_text(json.dumps({
            'source': 'fixture',
            'items': [{'id': 'wi-fixture', 'group': 'Fixture'}],
        }))

    def tearDown(self):
        self.tempdir.cleanup()

    def test_turn_archetype_wins_and_fallbacks_stay_explicit(self):
        item = read_snapshot(self.db, self.groups)['items'][0]
        stages = {stage['name']: stage for stage in item['stages']}

        self.assertEqual(stages['Spec']['turns'], 1)
        self.assertEqual(stages['Spec']['assignments'], 1)
        self.assertEqual(stages['Spec review']['turns'], 1)
        self.assertEqual(stages['Spec review']['assignments'], 1)
        self.assertEqual(stages['Coding']['turns'], 1)
        self.assertEqual(stages['Coding']['assignments'], 2)
        self.assertEqual(stages['Coding']['returns'], 1)
        self.assertEqual(stages['Code review']['turns'], 1)
        self.assertEqual(stages['Code review']['assignments'], 2)
        self.assertEqual(stages['Code review']['returns'], 1)
        self.assertEqual(stages['Unlabelled']['turns'], 1)
        self.assertEqual(stages['Unlabelled']['assignments'], 1)
        self.assertEqual(item['coordinationTurns'], 1)


if __name__ == '__main__':
    unittest.main()
