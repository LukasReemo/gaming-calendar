"""All event fixtures in this file describe a fictional game."""
import copy
from pathlib import Path
import tempfile
import unittest

from scripts.generate_calendar import ROOT, escape, generate, load_rows


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.games = [{"id": "fictional-game", "name": "Fictional Game",
                       "tracking_status": "interested", "tracked_event_types": ["release"],
                       "latest_news": ""}]
        self.events = [{"id": "fictional-release", "game_id": "fictional-game",
                        "title": "Release", "type": "release", "date": "2030-01-01",
                        "status": "confirmed", "source_url": "https://example.com/event",
                        "updated_at": "2026-10-05T12:00:00Z"}]

    def render(self):
        return generate(self.games, self.events)

    def unfolded(self):
        return self.render().replace("\r\n ", "")

    def test_production_yaml(self):
        games = load_rows(ROOT / "data/games.yaml", "games")
        events = load_rows(ROOT / "data/events.yaml", "events")
        calendar = generate(games, events)
        self.assertTrue(calendar.startswith("BEGIN:VCALENDAR\r\n"))
        self.assertTrue(calendar.endswith("END:VCALENDAR\r\n"))

    def test_required_fields_and_determinism(self):
        result = self.unfolded()
        for field in ["VERSION:2.0", "PRODID:", "UID:", "DTSTAMP:", "LAST-MODIFIED:",
                      "DTSTART;VALUE=DATE:20300101", "DTEND;VALUE=DATE:20300102",
                      "SUMMARY:🎮 Fictional Game · Release", "DESCRIPTION:", "STATUS:CONFIRMED"]:
            self.assertIn(field, result)
        self.assertEqual(self.render(), self.render())
        self.assertNotIn("\n", self.render().replace("\r\n", ""))

    def test_uid_survives_date_change(self):
        before = self.unfolded()
        self.events[0]["date"] = "2031-02-02"
        self.events[0]["updated_at"] = "2026-10-06T12:00:00Z"
        after = self.unfolded()
        uid = lambda text: next(line for line in text.splitlines() if line.startswith("UID:"))
        self.assertEqual(uid(before), uid(after))
        self.assertIn("DTSTART;VALUE=DATE:20310202", after)
        self.assertIn("LAST-MODIFIED:20261006T120000Z", after)
        self.assertIn("Last updated: 06 Oct 2026", after)

    def test_legacy_initialization_preserves_state_and_revisions(self):
        original = copy.deepcopy((self.games, self.events))
        result = self.unfolded()
        description = next(line for line in result.splitlines() if line.startswith("DESCRIPTION:"))
        self.assertTrue(description.endswith(r"\n\nLast updated: 05 Oct 2026"))
        self.assertIn("LAST-MODIFIED:20261005T120000Z", result)
        self.assertIn("DTSTAMP:20261005T120000Z", result)
        self.assertEqual(original, (self.games, self.events))

    def test_last_updated_prague_date_and_final_line_for_timed_events(self):
        self.events[0].pop("date")
        self.events[0].update(start_at="2030-01-01T12:00:00Z", end_at="2030-01-01T14:00:00Z",
                              source_timezone="UTC", notes="Fictional notes.")
        for revision, expected in [("2026-12-31T23:30:00Z", "01 Jan 2027"),
                                   ("2026-06-30T22:30:00Z", "01 Jul 2026"),
                                   ("2026-10-25T23:30:00Z", "26 Oct 2026")]:
            with self.subTest(revision=revision):
                self.events[0]["updated_at"] = revision
                description = next(line for line in self.unfolded().splitlines()
                                   if line.startswith("DESCRIPTION:"))
                self.assertIn("Source end:", description)
                self.assertTrue(description.endswith("Last updated: " + expected))
                self.assertEqual(description.count("Last updated:"), 1)

    def test_statuses_and_type_filter(self):
        for status, exported in [("confirmed", True), ("expected", True), ("rumored", False), ("delayed", False)]:
            with self.subTest(status=status):
                self.events[0]["status"] = status
                self.assertEqual("BEGIN:VEVENT" in self.render(), exported)
        self.events[0]["status"] = "expected"
        self.assertIn("STATUS:TENTATIVE", self.render())
        self.games[0]["tracked_event_types"] = []
        self.assertNotIn("BEGIN:VEVENT", self.render())

    def test_news_shared_and_revision(self):
        self.games[0].update(latest_news="A fictional announcement. More fictional news!",
                             latest_news_source_url="https://example.com/news",
                             latest_news_updated_at="2026-10-07T12:00:00Z")
        other = copy.deepcopy(self.events[0])
        other["id"] = "fictional-second-release"
        self.events.append(other)
        result = self.unfolded()
        self.assertEqual(result.count("Latest news:"), 2)
        self.assertEqual(result.count("News source:\\nhttps://example.com/news"), 2)
        self.assertEqual(result.count("LAST-MODIFIED:20261007T120000Z"), 2)
        self.assertEqual(result.count("Last updated: 07 Oct 2026"), 2)

    def test_absent_news_omits_sections(self):
        self.games[0]["latest_news_source_url"] = "https://example.com/news"
        result = self.unfolded()
        self.assertNotIn("Latest news:", result)
        self.assertNotIn("News source:", result)
        self.assertIn("Event source:\\nhttps://example.com/event", result)

    def test_escaping_and_utf8_folding(self):
        text = "comma, semicolon; backslash\\\r\nnext line"
        self.assertEqual(escape(text), "comma\\, semicolon\\; backslash\\\\\\nnext line")
        self.events[0]["notes"] = text + " 漢🎮" * 100
        result = self.render()
        for line in result.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75)
        self.assertIn(escape(self.events[0]["notes"]), self.unfolded())

    def test_validation(self):
        for field, value in [("game_id", "unknown"), ("date", "2030-02-30"),
                             ("status", "unknown"), ("type", "unknown"),
                             ("source_url", "javascript:alert(1)"), ("updated_at", "2026-10-05")]:
            with self.subTest(field=field):
                events = copy.deepcopy(self.events)
                events[0][field] = value
                with self.assertRaises(ValueError):
                    generate(self.games, events)
        self.games[0]["latest_news"] = "One. Two. Three. Four."
        with self.assertRaises(ValueError):
            self.render()

    def test_all_event_types_and_year_boundary(self):
        from scripts.generate_calendar import TYPES
        self.games[0]["tracked_event_types"] = list(TYPES)
        self.events[0]["date"] = "2030-12-31"
        for event_type, emoji in TYPES.items():
            with self.subTest(event_type=event_type):
                self.events[0]["type"] = event_type
                self.assertIn("SUMMARY:" + emoji, self.unfolded())
                self.assertIn("DTEND;VALUE=DATE:20310101", self.unfolded())

    def test_yaml_duplicate_ids_and_bad_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.yaml"
            for data in ["events: {}", "events:\n  - id: duplicate\n  - id: duplicate\n", "events: [broken"]:
                path.write_text(data, encoding="utf-8")
                with self.assertRaises((ValueError, __import__('yaml').YAMLError)):
                    load_rows(path, "events")


if __name__ == "__main__":
    unittest.main()
