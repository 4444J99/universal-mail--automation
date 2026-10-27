"""Cloudflare-only incident cohorts stay source-bound, private and additive."""
import hashlib
import json
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from core.historical_intelligence import build_historical_intelligence
from core.mail_action_plan import build_action_plan as build_mail_action_plan
from core.mail_history_export import build_mail_history_export, write_mail_history_export, MailHistoryExportError


class CloudflareIncidentTests(unittest.TestCase):
    def row(self, identity, subject, received='2026-10-18T17:00:00Z', sender='Cloudflare <notify@cloudflare.com>'):
        return {'id': identity, 'thread_id': identity, 'sender': sender,
                'subject': subject, 'received_at': received, 'direction': 'inbound'}

    def intelligence(self, rows):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.json'
            path.write_text(json.dumps({'messages': rows}))
            return build_historical_intelligence(path)

    def test_thresholds_and_exhaustion_share_one_incident_and_action(self):
        rows = [self.row(str(i), f'[Alert] D1 daily operation limit {level} for account: private-account')
                for i, level in enumerate(('75% reached', '91% reached', 'exceeded'))]
        rows[0]['body'] = 'Footer: security, payment, subscription renewal.'
        data = self.intelligence(rows)
        groups = data['provider_notice_groups']
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group['category'], 'usage_quota')
        self.assertEqual(group['resource'], 'd1')
        self.assertEqual(group['message_count'], 3)
        self.assertEqual(group['max_reported_percent'], 91)
        self.assertEqual(group['reported_levels'], ['exceeded', 'threshold'])
        self.assertEqual(len(group['evidence_ids']), 3)
        self.assertTrue(all(row['kind'] == 'provider_incident' for row in data['risks']))
        self.assertTrue(all(row['recommended_lane'] == 'provider_action' for row in data['risks']))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'intelligence.json'; path.write_text(json.dumps(data))
            actions = build_mail_action_plan(path)['items']
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]['finding_count'], 3)
        self.assertEqual(actions[0]['provider_notice']['incident_id'], group['incident_id'])
        self.assertNotIn('private-account', json.dumps(data))
        self.assertNotIn('Footer:', json.dumps(data))

    def test_resource_account_and_received_utc_date_remain_distinct(self):
        subject = '[Alert] D1 daily operation limit exceeded for account: one'
        rows = [self.row('a', subject, '2026-10-18T23:59:00Z'),
                self.row('b', subject, '2026-10-19T00:01:00Z'),
                self.row('c', subject.replace('D1', 'Workers KV'), '2026-10-18T23:59:00Z'),
                self.row('d', subject.replace('one', 'two'), '2026-10-18T23:59:00Z'),
                self.row('e', subject, '2026-10-18T19:59:00-04:00')]
        groups = self.intelligence(rows)['provider_notice_groups']
        self.assertEqual(len(groups), 4)
        self.assertEqual(sorted(g['message_count'] for g in groups), [1, 1, 1, 2])
        self.assertTrue(all(g['window_basis'] == 'received_utc_date_proxy' for g in groups))
        self.assertTrue(all(g['provider_usage_window'] is None for g in groups))
        self.assertEqual(groups, self.intelligence(list(reversed(rows)))['provider_notice_groups'])

    def test_missing_binding_or_date_cannot_merge_unrelated_notices(self):
        subject = '[Alert] Durable Objects daily operation limit exceeded'
        rows = [self.row('a', subject), self.row('b', subject),
                self.row('c', subject + ' for account: one', None),
                self.row('d', subject + ' for account: one', None)]
        groups = self.intelligence(rows)['provider_notice_groups']
        self.assertEqual(len(groups), 4)
        self.assertEqual(sum(g['account_binding'] == 'UNOBSERVED' for g in groups), 2)
        self.assertEqual(sum(g['received_utc_date'] is None for g in groups), 2)

    def test_daily_and_monthly_notices_do_not_share_an_incident(self):
        daily = '[Alert] D1 daily operation limit exceeded for account: one'
        groups = self.intelligence([self.row('a', daily), self.row('b', daily.replace('daily', 'monthly'))])['provider_notice_groups']
        self.assertEqual(len(groups), 2)
        self.assertEqual({g['reported_budget_period'] for g in groups}, {'daily', 'monthly'})

    def test_nonusage_categories_remain_separate_and_spoofs_do_not_become_provider_notices(self):
        subjects = ('Upgrade your Cloudflare Tunnel', 'Security alert: verify your Cloudflare account',
                    'Cloudflare invoice ready', 'Cloudflare subscription renewal', 'Cloudflare Connect invitation')
        rows = [self.row(str(i), subject) for i, subject in enumerate(subjects)]
        rows += [self.row('fake1', '[Alert] D1 daily operation limit exceeded', sender='x@cloudflare.com.evil.test'),
                 self.row('fake2', '[Alert] D1 daily operation limit exceeded', sender='x@notcloudflare.com')]
        groups = self.intelligence(rows)['provider_notice_groups']
        self.assertEqual(sorted(g['category'] for g in groups), ['billing', 'other', 'renewal', 'security', 'update'])
        self.assertEqual(sum(g['message_count'] for g in groups), 5)

    def test_existing_nonincident_action_identity_is_preserved(self):
        row = self.row('a', 'Cloudflare service outage', sender='human@example.test')
        data = self.intelligence([row])
        self.assertEqual(data['provider_notice_groups'], [])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'intel.json'; path.write_text(json.dumps(data))
            action = build_mail_action_plan(path)['items'][0]
        old_key = 'provider_incident|provider_action|ops_not_supplied|needs_portal_verification|medium'
        self.assertEqual(action['id'], 'action_' + hashlib.sha256(old_key.encode()).hexdigest()[:16])


class CloudflareIndexTests(unittest.TestCase):
    def index(self, directory):
        path = Path(directory) / 'Envelope Index'
        with sqlite3.connect(path) as connection:
            connection.executescript('''CREATE TABLE addresses (ROWID INTEGER PRIMARY KEY,address TEXT);
                CREATE TABLE subjects (ROWID INTEGER PRIMARY KEY,subject TEXT);
                CREATE TABLE messages (ROWID INTEGER PRIMARY KEY,sender INTEGER,subject INTEGER,
                    date_received INTEGER,conversation_id INTEGER,read INTEGER,body TEXT);''')
            connection.executemany('INSERT INTO addresses VALUES (?,?)',
                                   [(1, 'notify@cloudflare.com'), (2, 'x@cloudflare.com.evil.test'), (3, 'x@mail.cloudflare.com')])
            connection.executemany('INSERT INTO subjects VALUES (?,?)', [(1, '[Alert] D1 daily operation limit exceeded for account: private'), (2, 'Cloudflare invoice')])
            connection.executemany('INSERT INTO messages VALUES (?,?,?,?,?,?,?)',
                                   [(1, 1, 1, 1792314000, 1, 0, 'private-body'),
                                    (2, 2, 1, 1792314001, 2, 0, 'spoof-body'),
                                    (3, 3, 2, 1792400400, 3, 1, 'private-body')])
        return path

    def test_index_export_is_bounded_metadata_only_and_does_not_change_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.index(directory); before = path.read_bytes()
            data = build_mail_history_export(path, source_type='apple_mail_cloudflare_index')
            self.assertEqual(len(data['messages']), 2)
            self.assertTrue(data['source']['coverage']['complete_for_local_query'])
            self.assertTrue(all(row['body'] is None for row in data['messages']))
            self.assertNotIn('private-body', json.dumps(data))
            self.assertNotIn('spoof-body', json.dumps(data))
            self.assertEqual(path.read_bytes(), before)

    def test_explicit_limit_is_visible_and_date_filter_applies_before_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.index(directory)
            data = build_mail_history_export(path, source_type='apple_mail_cloudflare_index', limit=1)
            self.assertFalse(data['source']['coverage']['complete_for_local_query'])
            self.assertEqual(data['source']['coverage']['matching_rows'], 2)
            data = build_mail_history_export(path, source_type='apple_mail_cloudflare_index', since='2026-10-19', limit=1)
            self.assertTrue(data['source']['coverage']['complete_for_local_query'])
            self.assertEqual(len(data['messages']), 1)

    def test_schema_failure_is_sanitized_and_raw_export_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'private-source-name'; sqlite3.connect(path).close()
            with self.assertRaises(MailHistoryExportError) as caught:
                build_mail_history_export(path, source_type='apple_mail_cloudflare_index')
            self.assertNotIn(str(path), str(caught.exception))
            output = Path(directory) / 'history.json'; output.write_text('{}'); output.chmod(0o644)
            receipt = write_mail_history_export({'messages': [{'subject': 'private-subject'}]}, output)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertNotIn('private-subject', json.dumps(receipt))


if __name__ == '__main__': unittest.main()
