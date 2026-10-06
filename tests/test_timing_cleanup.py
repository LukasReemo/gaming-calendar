"""Fictional fixtures for exact timing and snapshot cleanup."""
import copy
from datetime import timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

from scripts.generate_calendar import ROOT, check_managed_feed, event_timing, generate, remove_tracked_game
import test_calendar


class TimingCleanupTests(unittest.TestCase):
    def setUp(self):
        fixture = test_calendar.CalendarTests()
        fixture.setUp()
        self.games, self.events = fixture.games, fixture.events

    def timed(self, start, zone=None, end=None):
        event = self.events[0]
        event.pop('date', None)
        event['start_at'] = start
        if zone:
            event['source_timezone'] = zone
        if end:
            event['end_at'] = end
        return generate(self.games, self.events).replace('\r\n ', '')

    def test_date_only(self):
        result = generate(self.games, self.events)
        self.assertIn('DTSTART;VALUE=DATE:20300101', result)
        self.assertNotIn('BEGIN:VTIMEZONE', result)

    def test_prague_exact_and_source_metadata(self):
        result = self.timed('2026-12-11T17:00:00', 'Europe/Prague')
        self.assertIn('DTSTART;TZID=Europe/Prague:20261211T170000', result)
        self.assertIn('DTEND;TZID=Europe/Prague:20261211T180000', result)
        self.assertIn('Source timezone: Europe/Prague', result)
        self.assertIn('BEGIN:VTIMEZONE', result)
        self.assertIn('X-GAMING-CALENDAR-GAME-ID:fictional-game', result)

    def test_us_time_and_dst_mismatch(self):
        for source, expected in [('2026-12-11T08:00:00', '20261211T170000'),
                                 ('2026-07-11T08:00:00', '20260711T170000'),
                                 ('2026-03-15T08:00:00', '20260315T160000')]:
            with self.subTest(source=source):
                self.assertIn('DTSTART;TZID=Europe/Prague:' + expected,
                              self.timed(source, 'America/Los_Angeles'))

    def test_known_end_and_midnight(self):
        result = self.timed('2026-10-29T22:00:00Z', 'UTC', '2026-10-30T01:30:00Z')
        self.assertIn('DTSTART;TZID=Europe/Prague:20261029T230000', result)
        self.assertIn('DTEND;TZID=Europe/Prague:20261030T023000', result)

    def test_prague_dst_transition_duration_and_fold(self):
        self.timed('2026-03-29T01:30:00', 'Europe/Prague')
        kind, start, end = event_timing(self.events[0])
        self.assertEqual(end.hour, 3)
        self.assertEqual(end.astimezone(timezone.utc) - start.astimezone(timezone.utc), timedelta(hours=1))
        self.events[0]['start_at'] = '2026-10-25T02:30:00+01:00'
        result = generate(self.games, self.events)
        self.assertIn('DTSTART:20261025T013000Z', result)

    def test_invalid_timing(self):
        for fields in [{'start_at': '2026-12-11'}, {'start_at': '2026-12-11T17:00'},
                       {'start_at': '2026-03-29T02:30', 'source_timezone': 'Europe/Prague'},
                       {'start_at': '2026-10-25T02:30', 'source_timezone': 'Europe/Prague'},
                       {'start_at': '2026-12-11T17:00+02:00', 'source_timezone': 'Europe/Prague'},
                       {'start_at': '2026-12-11T17:00Z', 'end_at': '2026-12-11T16:00Z'},
                       {'end_at': '2026-12-11T17:00Z'}]:
            with self.subTest(fields=fields):
                event = copy.deepcopy(self.events[0])
                event.pop('date')
                event.update(fields)
                with self.assertRaises(ValueError):
                    generate(self.games, [event])
        self.events[0]['start_at'] = '2026-12-11T17:00Z'
        with self.assertRaises(ValueError):
            generate(self.games, self.events)

    def test_same_uid_when_time_changes(self):
        before = generate(self.games, self.events)
        after = self.timed('2030-01-01T17:00:00', 'Europe/Prague')
        uid = 'UID:fictional-release@gaming-calendar'
        self.assertIn(uid, before)
        self.assertIn(uid, after)
        self.assertEqual(after.count(uid), 1)
        self.assertEqual(after, generate(self.games, self.events).replace('\r\n ', ''))

    def test_removal_preserves_other_game_with_same_display_name(self):
        other_game = dict(self.games[0], id='other-game')
        other_event = dict(self.events[0], id='other-release', game_id='other-game')
        self.games.append(other_game)
        self.events.append(other_event)
        games, events = remove_tracked_game(self.games, self.events, 'fictional-game')
        result = generate(games, events)
        self.assertNotIn('UID:fictional-release@', result)
        self.assertIn('UID:other-release@', result)
        self.assertEqual((games, events), remove_tracked_game(games, events, 'fictional-game'))

    def test_cli_repeated_cleanup_does_not_touch_manual_calendar(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            games, events, output = base/'games.yaml', base/'events.yaml', base/'feed.ics'
            games.write_text(yaml.safe_dump({'games': self.games}))
            events.write_text(yaml.safe_dump({'events': self.events}))
            manual = base/'manual.ics'
            manual.write_text('BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:manual\nEND:VEVENT\nEND:VCALENDAR\n')
            original = manual.read_bytes()
            command = [sys.executable, str(ROOT/'scripts/generate_calendar.py'),
                       '--games', str(games), '--events', str(events), '--output', str(output),
                       '--remove-game', 'fictional-game']
            for _ in range(2):
                subprocess.run(command, check=True, capture_output=True)
                self.assertNotIn('BEGIN:VEVENT', output.read_text())
                self.assertEqual(manual.read_bytes(), original)
                self.assertEqual(yaml.safe_load(games.read_text()), {'games': []})
                self.assertEqual(yaml.safe_load(events.read_text()), {'events': []})

    def test_cleanup_rejects_manual_or_mixed_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/"calendar.ics"
            for content in ["BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:manual\nEND:VEVENT\nEND:VCALENDAR\n",
                            generate(self.games, self.events).replace("END:VCALENDAR", "BEGIN:VEVENT\r\nUID:manual\r\nEND:VEVENT\r\nEND:VCALENDAR")]:
                output.write_text(content)
                original = output.read_bytes()
                with self.assertRaises(ValueError):
                    check_managed_feed(output)
                self.assertEqual(original, output.read_bytes())
            output.write_text(generate(self.games, self.events))
            check_managed_feed(output)
