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
            [
                ('wi-fixture', 'Fixture item', 'open', 1),
                ('wi-single', 'Single archetype', 'open', 2),
                ('wi-two', 'Two archetypes', 'open', 3),
                ('wi-empty', 'Empty item', 'open', 4),
            ],
        )
        connection.executemany(
            'INSERT INTO sessions VALUES (?, ?)',
            [
                ('s-spec', 'spec-writer'),
                ('s-coder', 'coder'),
                ('s-review-spec', 'reviewer-spec'),
                ('s-review-code', 'reviewer-code'),
                ('s-orchestrator', 'orchestrator'),
                ('s-unknown', 'default'),
                ('s-missing', None),
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
                ('a-review', 'wi-fixture', 's-missing', 'reviewer', 'a-coder', 4, 'closed'),
                ('a-spec-review', 'wi-fixture', 's-review-spec', None, 'a-spec', 5, 'closed'),
                # A later review is the successor after the adverse review.
                ('a-retry', 'wi-fixture', 's-missing', 'reviewer', 'a-coder', 9, 'closed'),
                # No archetype, role, or review link remains Unknown.
                ('a-unknown', 'wi-fixture', 's-missing', None, None, 6, 'closed'),
                ('a-default', 'wi-fixture', 's-unknown', None, None, 7, 'closed'),
                ('a-single', 'wi-single', 's-coder', None, None, 10, 'closed'),
                ('a-two-coder', 'wi-two', 's-coder', None, None, 11, 'closed'),
                ('a-two-review', 'wi-two', 's-review-code', None, None, 12, 'closed'),
            ],
        )
        connection.executemany(
            'INSERT INTO turns VALUES (?, ?, ?, ?, ?, ?, ?)',
            [
                (1, 'a-spec', 's-spec', None, 'delivered', 10, 20),
                (2, 'a-coder', 's-coder', None, 'delivered', 30, 40),
                (3, 'a-supervised', 's-orchestrator', 'coder', 'delivered', 50, 60),
                (4, 'a-review', 's-missing', None, 'delivered', 70, 80),
                (5, 'a-spec-review', 's-review-spec', None, 'delivered', 90, 100),
                (6, 'a-unknown', 's-missing', None, 'delivered', 110, 120),
                (7, 'a-default', 's-unknown', None, 'delivered', 130, 140),
                (8, 'a-single', 's-coder', None, 'delivered', 150, 160),
                (9, 'a-two-coder', 's-coder', None, 'delivered', 170, 180),
                (10, 'a-two-review', 's-review-code', None, 'delivered', 190, 200),
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
            'items': [
                {'id': 'wi-fixture', 'group': 'Fixture', 'summary': 'Direct summary <unsafe>',
                 'releaseStatus': 'Landed', 'checkedAt': '2026-09-15', 'scope': 'Included'},
                {'id': 'wi-single', 'group': 'Fixture', 'description': 'Direct description',
                 'releaseStatus': 'Remaining'},
                {'id': 'wi-two', 'group': 'Fixture', 'releaseStatus': 'invalid'},
                {'id': 'wi-empty', 'group': 'Fixture'},
            ],
        }))

    def tearDown(self):
        self.tempdir.cleanup()

    def test_turn_archetype_wins_and_fallbacks_stay_explicit(self):
        snapshot = read_snapshot(self.db, self.groups)
        items = {item['id']: item for item in snapshot['items']}
        self.assertEqual(
            snapshot['archetypes'],
            ['coder', 'default', 'orchestrator', 'reviewer-code', 'reviewer-spec', 'spec-writer', 'Unknown'],
        )
        self.assertEqual(items['wi-empty']['stages'], [])
        self.assertEqual(items['wi-fixture']['description'], 'Direct summary <unsafe>')
        self.assertEqual(items['wi-fixture']['descriptionSource'], 'Groups configuration')
        self.assertEqual(items['wi-single']['description'], 'Direct description')
        self.assertEqual(items['wi-single']['descriptionSource'], 'Groups configuration')
        self.assertEqual(items['wi-two']['description'], 'No description recorded')
        self.assertEqual(items['wi-two']['descriptionSource'], 'No description recorded')
        self.assertEqual(items['wi-fixture']['releaseStatus'], 'Landed')
        self.assertEqual(items['wi-fixture']['checkedAt'], '2026-09-15')
        self.assertEqual(items['wi-fixture']['scope'], 'Included')
        self.assertEqual(items['wi-single']['releaseStatus'], 'Remaining')
        self.assertIsNone(items['wi-single']['checkedAt'])
        self.assertEqual(items['wi-two']['releaseStatus'], 'Scope unresolved')
        self.assertIsNone(items['wi-empty']['checkedAt'])
        self.assertIsNone(items['wi-empty']['scope'])
        self.assertEqual([s['name'] for s in items['wi-single']['stages']], ['coder'])
        self.assertEqual([s['name'] for s in items['wi-two']['stages']], ['coder', 'reviewer-code'])

        item = items['wi-fixture']
        stages = {stage['name']: stage for stage in item['stages']}

        self.assertEqual(stages['spec-writer']['turns'], 1)
        self.assertEqual(stages['spec-writer']['assignments'], 1)
        self.assertEqual(stages['reviewer-spec']['turns'], 1)
        self.assertEqual(stages['reviewer-spec']['assignments'], 1)
        self.assertEqual(stages['coder']['turns'], 1)
        self.assertEqual(stages['coder']['assignments'], 2)
        self.assertEqual(stages['coder']['returns'], 1)
        self.assertEqual(stages['orchestrator']['turns'], 1)
        self.assertEqual(stages['orchestrator']['assignments'], 0)
        self.assertEqual(stages['default']['turns'], 1)
        self.assertEqual(stages['default']['assignments'], 1)
        self.assertEqual(stages['Unknown']['turns'], 2)
        self.assertEqual(stages['Unknown']['assignments'], 3)
        self.assertEqual(stages['Unknown']['returns'], 1)
        self.assertEqual(item['coordinationTurns'], 1)

    def test_inventory_summary_is_the_display_description(self):
        root = Path(self.tempdir.name)
        inventory = root / 'inventory.json'
        inventory.write_text(json.dumps({'items': [
            {'id': 'wi-fixture', 'category': 'Fixture', 'summary': 'Inventory summary',
             'releaseStatus': 'Standing ownership', 'checkedAt': '2026-09-14T12:00:00Z',
             'scope': 'Included'},
            {'id': 'wi-empty', 'category': 'Fixture'},
        ]}))
        groups = root / 'inventory-groups.json'
        groups.write_text(json.dumps({
            'source': 'inventory fixture',
            'inventoryPath': inventory.name,
        }))
        snapshot = read_snapshot(self.db, groups)
        items = {item['id']: item for item in snapshot['items']}
        self.assertEqual(items['wi-fixture']['description'], 'Inventory summary')
        self.assertEqual(items['wi-fixture']['descriptionSource'], 'Inventory summary')
        self.assertEqual(items['wi-fixture']['releaseStatus'], 'Standing ownership')
        self.assertEqual(items['wi-fixture']['checkedAt'], '2026-09-14T12:00:00Z')
        self.assertEqual(items['wi-fixture']['scope'], 'Included')
        self.assertEqual(items['wi-empty']['description'], 'No description recorded')
        self.assertEqual(items['wi-empty']['releaseStatus'], 'Scope unresolved')


if __name__ == '__main__':
    unittest.main()
