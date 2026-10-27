"""Cloudflare-only incident cohorts stay source-bound, private and additive."""
import argparse
import contextlib
import hashlib
import io
import os
import json
import sqlite3
import stat
import tempfile
import unittest
from unittest import mock
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

    def export_cli(self, source, output):
        from cli import cmd_mail_history_export

        args = argparse.Namespace(source=str(source), output=str(output),
            source_type='apple_mail_cloudflare_index', since=None, until=None,
            limit=None, body_char_limit=0, self_address=[], mailbox_hint=None,
            pretty_export=False, sort_keys=False, pretty=False)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = cmd_mail_history_export(args)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_date_bounds_preserve_undated_notices_and_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.index(directory)
            with sqlite3.connect(path) as connection:
                connection.execute('INSERT INTO messages VALUES (4,1,1,NULL,4,0,?)', ('private-undated-body',))
            for bounds in ({'since': '2026-10-19'}, {'until_exclusive': '2026-10-19'},
                           {'since': '2026-10-19', 'until_exclusive': '2026-10-20'}):
                with self.subTest(bounds=bounds):
                    data = build_mail_history_export(path, source_type='apple_mail_cloudflare_index', **bounds)
                    coverage = data['source']['coverage']
                    self.assertEqual(coverage['matching_rows'], 2)
                    self.assertEqual(coverage['returned_rows'], 2)
                    self.assertEqual(coverage['unknown_received_dates'], 1)
                    self.assertTrue(coverage['complete_for_local_query'])
                    self.assertEqual(sum(row['received_at'] is None for row in data['messages']), 1)
                    limited = build_mail_history_export(path, source_type='apple_mail_cloudflare_index', limit=1, **bounds)
                    self.assertFalse(limited['source']['coverage']['complete_for_local_query'])
                    self.assertEqual(limited['source']['coverage']['matching_rows'], 2)
                    self.assertEqual(limited['source']['coverage']['unknown_received_dates'], 1)

    def test_cli_rejects_source_output_aliases_without_modifying_index(self):
        for alias in ('same_path', 'symlink', 'hardlink'):
            with self.subTest(alias=alias), tempfile.TemporaryDirectory() as directory:
                source = self.index(directory)
                output = source if alias == 'same_path' else Path(directory) / 'export.json'
                if alias == 'symlink':
                    output.symlink_to(source)
                elif alias == 'hardlink':
                    output.hardlink_to(source)
                before = source.read_bytes()
                mode_before = stat.S_IMODE(source.stat().st_mode)
                result, stdout, stderr = self.export_cli(source, output)
                self.assertEqual(result, 1)
                self.assertIn('output must not identify the mail history source', stderr)
                self.assertEqual(stdout, '')
                self.assertNotIn(str(source), stderr)
                self.assertEqual(source.read_bytes(), before)
                self.assertEqual(stat.S_IMODE(source.stat().st_mode), mode_before)

    def test_writer_rejects_alias_introduced_during_export_build(self):
        for alias in ('symlink', 'hardlink', 'replaced_source', 'original_source'):
            with self.subTest(alias=alias), tempfile.TemporaryDirectory() as directory:
                source = self.index(directory)
                output = Path(directory) / 'export.json'
                original = Path(directory) / 'original-index'
                before = source.read_bytes()
                mode_before = stat.S_IMODE(source.stat().st_mode)

                def build_then_alias(*args, **kwargs):
                    export = build_mail_history_export(*args, **kwargs)
                    target = source
                    if alias in ('replaced_source', 'original_source'):
                        source.rename(original)
                        self.index(directory)
                        if alias == 'original_source':
                            target = original
                    if alias == 'symlink':
                        output.symlink_to(target)
                    else:
                        output.hardlink_to(target)
                    return export

                with mock.patch('core.mail_history_export.build_mail_history_export', side_effect=build_then_alias):
                    result, stdout, stderr = self.export_cli(source, output)
                self.assertEqual(result, 1)
                self.assertIn('output must not identify the mail history source', stderr)
                self.assertEqual(stdout, '')
                self.assertNotIn(str(source), stderr)
                self.assertEqual(source.read_bytes(), before)
                self.assertEqual(stat.S_IMODE(source.stat().st_mode), mode_before)
                if original.exists():
                    self.assertEqual(original.read_bytes(), before)
                    self.assertEqual(stat.S_IMODE(original.stat().st_mode), mode_before)

    def test_writer_rejects_replaced_destination_without_success_receipt(self):
        for phase in ('open', 'truncate'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as directory:
                source = self.index(directory)
                output = Path(directory) / 'export.json'
                before = source.read_bytes()
                mode_before = stat.S_IMODE(source.stat().st_mode)
                real_operation = os.open if phase == 'open' else os.ftruncate

                def replace_destination(*args, **kwargs):
                    result = real_operation(*args, **kwargs)
                    output.unlink()
                    output.symlink_to(source)
                    return result

                with mock.patch('core.mail_history_export.os.' + ('open' if phase == 'open' else 'ftruncate'),
                                side_effect=replace_destination):
                    result, stdout, stderr = self.export_cli(source, output)
                self.assertEqual(result, 1)
                self.assertIn('mail history export destination changed during writing', stderr)
                self.assertEqual(stdout, '')
                self.assertEqual(source.read_bytes(), before)
                self.assertEqual(stat.S_IMODE(source.stat().st_mode), mode_before)

    def test_index_writer_requires_complete_source_binding_before_open(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.index(directory)
            source_stat = source.stat()
            identity = (source_stat.st_dev, source_stat.st_ino)
            export = build_mail_history_export(source, source_type='apple_mail_cloudflare_index')
            before = source.read_bytes()
            mode_before = stat.S_IMODE(source.stat().st_mode)
            for binding in ({}, {'source_path': source}, {'source_identity': identity}):
                with self.subTest(binding=tuple(binding)):
                    with self.assertRaisesRegex(MailHistoryExportError, 'require source_path and source_identity'):
                        write_mail_history_export(export, source, **binding)
                    destination = Path(directory) / 'absent-parent' / 'history.json'
                    with self.assertRaisesRegex(MailHistoryExportError, 'require source_path and source_identity'):
                        write_mail_history_export(export, destination, **binding)
                    self.assertFalse(destination.parent.exists())
                    self.assertEqual(source.read_bytes(), before)
                    self.assertEqual(stat.S_IMODE(source.stat().st_mode), mode_before)
            output = Path(directory) / 'history.json'
            receipt = write_mail_history_export(export, output, source_path=source, source_identity=identity)
            self.assertEqual(json.loads(output.read_text()), export)
            self.assertEqual(receipt['output']['message_count'], 2)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertNotIn('source_identity', json.dumps(receipt))
            self.assertEqual(source.read_bytes(), before)

    def test_index_writer_protects_sqlite_companions_and_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.index(directory)
            writer = sqlite3.connect(source)
            try:
                self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
                writer.execute('PRAGMA wal_autocheckpoint=0')
                writer.execute('INSERT INTO messages VALUES (4,1,1,1792400401,4,0,?)', ('private-wal-body',))
                writer.commit()
                source_alias = Path(directory) / 'source-link'
                source_alias.symlink_to(source)
                source_stat = source.stat()
                identity = (source_stat.st_dev, source_stat.st_ino)
                export = build_mail_history_export(source_alias, source_type='apple_mail_cloudflare_index')
                journal = Path(str(source) + '-journal')
                journal.write_bytes(b'synthetic rollback-journal sentinel')
                protected = [source] + [Path(str(source) + suffix) for suffix in ('-wal', '-shm', '-journal')]
                before = {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in protected}
                for companion in protected[1:]:
                    for alias in ('same_path', 'symlink', 'hardlink'):
                        with self.subTest(companion=companion.suffix, alias=alias):
                            output = companion if alias == 'same_path' else Path(directory) / 'export.json'
                            if alias == 'symlink':
                                output.symlink_to(companion)
                            elif alias == 'hardlink':
                                output.hardlink_to(companion)
                            try:
                                with self.assertRaisesRegex(MailHistoryExportError, 'output must not identify'):
                                    write_mail_history_export(export, output,
                                                              source_path=source_alias, source_identity=identity)
                                for path, snapshot in before.items():
                                    self.assertEqual((path.read_bytes(), stat.S_IMODE(path.stat().st_mode)), snapshot)
                            finally:
                                if alias != 'same_path':
                                    output.unlink()
            finally:
                writer.close()

    def test_index_writer_rejects_absent_companion_names_without_creating_them(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.index(directory)
            source_stat = source.stat()
            identity = (source_stat.st_dev, source_stat.st_ino)
            export = build_mail_history_export(source, source_type='apple_mail_cloudflare_index')
            for suffix in ('-wal', '-shm', '-journal'):
                with self.subTest(suffix=suffix):
                    destination = Path(str(source) + suffix)
                    self.assertFalse(destination.exists())
                    with self.assertRaisesRegex(MailHistoryExportError, 'output must not identify'):
                        write_mail_history_export(export, destination,
                                                  source_path=source, source_identity=identity)
                    self.assertFalse(destination.exists())

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

    def test_nonpositive_public_limits_are_rejected_before_index_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.index(directory)
            for bound in (0, -1):
                with self.assertRaisesRegex(MailHistoryExportError, 'limit must be greater than zero'):
                    build_mail_history_export(path, source_type='apple_mail_cloudflare_index', limit=bound)

    def test_committed_wal_rows_are_visible_without_copying_or_mutating_mail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.index(directory)
            writer = sqlite3.connect(path)
            try:
                self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
                writer.execute('PRAGMA wal_autocheckpoint=0')
                writer.execute('INSERT INTO messages VALUES (4,1,1,1792400401,4,0,?)', ('private-wal-body',))
                writer.commit()
                wal = Path(str(path) + '-wal')
                before = (path.read_bytes(), wal.read_bytes())
                data = build_mail_history_export(path, source_type='apple_mail_cloudflare_index')
                self.assertEqual(len(data['messages']), 3)
                self.assertEqual((path.read_bytes(), wal.read_bytes()), before)
                self.assertNotIn('private-wal-body', json.dumps(data))
            finally:
                writer.close()

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
