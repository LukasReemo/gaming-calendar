"""Deterministic sync guardrails; every research fixture is fictional."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from scripts.generate_calendar import ROOT, generate
from scripts.sync_calendar import (DEFAULT_STATE, apply_sync, cleanup_old_events, instant,
                                   list_games, read_store, recover, save_store, upcoming_events)

NOW = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.games = [{"id": "fictional-game", "name": "Fictional Game", "tracking_status": "interested",
                       "tracked_event_types": ["release", "major_update"], "latest_news": "Old news."}]
        self.events = [{"id": "fictional-release", "game_id": "fictional-game", "title": "Release",
                        "type": "release", "date": "2026-11-01", "status": "confirmed",
                        "source_url": "https://example.com/release", "updated_at": "2026-10-01T12:00:00Z"}]
        self.state = copy.deepcopy(DEFAULT_STATE)
        self.result = {"game_id": "fictional-game", "status": "OK", "latest_news": "New news.",
                       "latest_news_source_url": "https://example.com/news", "events": copy.deepcopy(self.events)}

    def apply(self, results=None, **kwargs):
        return apply_sync(self.games, self.events, self.state,
                          {"games": [self.result] if results is None else results}, NOW, **kwargs)

    def second_game(self):
        self.games.append(dict(self.games[0], id="second-game", name="Second Game"))
        self.events.append(dict(self.events[0], id="second-release", game_id="second-game"))

    def test_stable_uid_date_and_time_updates(self):
        self.result["events"][0]["date"] = "2026-11-08"
        games, events, state, summary = self.apply()
        self.assertEqual(summary["events_updated"], 1)
        self.assertEqual(summary["events_added"], 0)
        self.assertIn("UID:fictional-release@gaming-calendar", generate(games, events))
        self.events = events
        self.result["events"][0].pop("date")
        self.result["events"][0]["start_at"] = "2026-11-08T16:00:00Z"
        games, events, _, _ = self.apply()
        self.assertEqual(len(events), 1)
        self.assertIn("DTSTART;TZID=Europe/Prague:20261108T170000", generate(games, events))
        self.assertIn("UID:fictional-release@gaming-calendar", generate(games, events))

    def test_later_full_sync_without_changes_does_not_checkpoint(self):
        after = self.apply(full=True)
        later = NOW + timedelta(days=3)
        replay = apply_sync(*after[:3], {"games": [self.result]}, later, full=True)
        self.assertEqual(after[:3], replay[:3])
        self.assertEqual(replay[3]["games"][0]["attempted_at"], later.isoformat())
        self.assertFalse(replay[3]["games"][0]["checkpoint_updated"])
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self.write_store(base)
            save_store(base, read_store(base), after[:3])
            original = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in base.iterdir()}
            proposal = base / "proposal.json"
            proposal.write_text(json.dumps({"games": [self.result]}))
            command = [sys.executable, str(ROOT / "scripts/sync_calendar.py"), "--data-dir", str(base),
                       "--now", later.isoformat(), "apply", "--full", "--proposal", str(proposal)]
            result = subprocess.run(command, check=True, capture_output=True, text=True)
            self.assertFalse(json.loads(result.stdout)["changed"])
            for name, snapshot in original.items():
                path = base / name
                self.assertEqual(snapshot, (path.read_bytes(), path.stat().st_mtime_ns))

    def test_failure_changes_and_recovery_checkpoint_but_repeat_failure_does_not(self):
        successful = self.apply()
        failure = {"games": [{"game_id": "fictional-game", "status": "failed", "message": "Source unreachable"}]}
        failed_at = NOW + timedelta(days=1)
        failed = apply_sync(*successful[:3], failure, failed_at, full=True)
        meta = failed[2]["games"]["fictional-game"]
        self.assertEqual(meta["status"], "failed")
        self.assertEqual(meta["last_attempt"], failed_at.isoformat())
        self.assertEqual(meta["last_success"], NOW.isoformat())
        repeated = apply_sync(*failed[:3], failure, failed_at + timedelta(days=1), full=True)
        self.assertEqual(failed[:3], repeated[:3])
        failure["games"][0]["message"] = "Source accessible but invalid response"
        changed = apply_sync(*repeated[:3], failure, failed_at + timedelta(days=2), full=True)
        self.assertNotEqual(changed[2], repeated[2])
        recovered_at = failed_at + timedelta(days=3)
        recovered = apply_sync(*changed[:3], {"games": [self.result]}, recovered_at, full=True)
        self.assertEqual(recovered[:2], successful[:2])
        self.assertEqual(recovered[2]["games"]["fictional-game"]["status"], "OK")
        self.assertEqual(recovered[2]["games"]["fictional-game"]["last_success"], recovered_at.isoformat())

    def test_warning_transition_and_retention_are_meaningful_checkpoints(self):
        successful = self.apply()
        warning = {"games": [dict(self.result, status="warning", message="Incomplete sources")]}
        later = NOW + timedelta(days=1)
        incomplete = apply_sync(*successful[:3], warning, later, full=True)
        self.assertEqual(incomplete[2]["games"]["fictional-game"]["status"], "warning")
        repeated = apply_sync(*incomplete[:3], warning, later + timedelta(days=1), full=True)
        self.assertEqual(incomplete[:3], repeated[:3])
        expired = apply_sync(*successful[:3], {"games": [dict(self.result, events=[])]},
                             NOW + timedelta(days=90), full=True)
        self.assertEqual(expired[1], [])
        self.assertTrue(expired[3]["games"][0]["checkpoint_updated"])
        self.assertEqual(expired[3]["retention_removed"], 1)

    def test_logical_key_is_stable_and_legacy_key_can_use_id(self):
        event = dict(self.events[0], logical_key="fictional-release", date="2026-11-09")
        event.pop("id")
        self.result["events"] = [event]
        _, events, _, _ = self.apply()
        self.assertEqual(events[0]["id"], "fictional-release")
        self.events = events
        self.result["events"][0]["title"] = "Renamed Release"
        self.result["events"][0]["date"] = "2026-11-10"
        _, events, _, _ = self.apply()
        self.assertEqual(events[0]["id"], "fictional-release")

    def test_new_key_deterministic_id(self):
        self.result["events"] = [dict(self.events[0], logical_key="update-one", title="Update One", type="major_update")]
        self.result["events"][0].pop("id")
        _, events, _, summary = self.apply()
        self.assertIn("fictional-game-update-one", {e["id"] for e in events})
        self.assertEqual(summary["events_added"], 1)

    def test_duplicate_identity_is_rejected_and_other_game_continues(self):
        self.second_game()
        self.result["events"][0]["id"] = "different-id"
        other = dict(self.result, game_id="second-game", events=[])
        games, events, state, summary = self.apply([self.result, other])
        self.assertEqual(state["games"]["fictional-game"]["status"], "failed")
        self.assertEqual(state["games"]["second-game"]["status"], "OK")
        self.assertEqual(summary["failed_games"], 1)
        self.assertEqual(events, self.events)
        self.assertEqual(games[0], self.games[0])

    def test_cross_game_id_collision_is_isolated(self):
        self.second_game()
        self.result["events"] = [dict(self.events[0], id="second-release", title="Another Release")]
        _, events, state, _ = self.apply()
        self.assertEqual(events, self.events)
        self.assertEqual(state["games"]["fictional-game"]["status"], "failed")

    def test_omission_never_deletes_and_failure_preserves_news(self):
        self.result["events"] = []
        games, events, state, _ = self.apply()
        self.assertEqual(events, self.events)
        self.assertEqual(games[0]["latest_news"], "New news.")
        self.result = {"game_id": "fictional-game", "status": "failed", "message": "Source unreachable"}
        games, events, state, _ = self.apply()
        self.assertEqual(games, self.games)
        self.assertEqual(events, self.events)
        self.assertNotIn("last_success", state["games"]["fictional-game"])

    def test_missing_full_sync_result_records_failure(self):
        self.second_game()
        _, events, state, summary = self.apply(full=True)
        self.assertEqual(events, self.events)
        self.assertEqual(summary["games_processed"], 2)
        self.assertEqual(summary["events_removed"], 0)
        self.assertEqual(state["games"]["second-game"]["status"], "failed")

    def test_last_success_survives_failed_or_incomplete_attempt(self):
        self.state["games"]["fictional-game"] = {"last_attempt": "2026-10-01T12:00:00Z",
                                                 "last_success": "2026-10-01T12:00:00Z", "status": "OK"}
        for status in ("warning", "failed"):
            result = dict(self.result, status=status, message="Incomplete")
            if status == "failed":
                result = {"game_id": "fictional-game", "status": status}
            _, _, state, _ = self.apply([result])
            self.assertEqual(state["games"]["fictional-game"]["last_success"], "2026-10-01T12:00:00Z")
            self.assertEqual(state["games"]["fictional-game"]["last_attempt"], NOW.isoformat())

    def test_invalid_dates_times_news_and_schema_are_isolated(self):
        for change in ({"date": "early 2027"}, {"start_at": "2026-11-01T12:00:00"},
                       {"importance": 7}, {"arbitrary": True}, {"status": "expected"},
                       {"game_id": "another-game"}):
            with self.subTest(change=change):
                result = copy.deepcopy(self.result)
                if "start_at" in change:
                    result["events"][0].pop("date")
                result["events"][0].update(change)
                games, events, state, _ = self.apply([result])
                self.assertEqual(events, self.events)
                self.assertEqual(games, self.games)
                self.assertEqual(state["games"]["fictional-game"]["status"], "failed")
        self.result["latest_news"] = "One. Two. Three. Four."
        self.assertEqual(self.apply()[2]["games"]["fictional-game"]["status"], "failed")
        self.result["latest_news"] = None
        self.assertEqual(self.apply()[2]["games"]["fictional-game"]["status"], "failed")

    def test_tentative_exact_date_stays_out_of_ics(self):
        self.result["events"][0]["status"] = "tentative"
        games, events, _, _ = self.apply()
        self.assertEqual(len(events), 1)
        self.assertNotIn("BEGIN:VEVENT", generate(games, events))

    def test_capacity_and_nearest_importance_selection(self):
        self.result["events"] = [dict(self.events[0], id=f"event-{i}", title=f"Release {i}",
                                      date=f"2026-10-{10+i:02}") for i in range(7)]
        _, events, _, _ = self.apply()
        self.assertEqual({e["id"] for e in events}, {f"event-{i}" for i in range(5)})
        self.result["events"][5].update(date="2026-10-10", importance=3)
        _, events, _, _ = self.apply()
        self.assertIn("event-5", {e["id"] for e in events})
        with self.assertRaisesRegex(ValueError, "Maximum 5"):
            generate(self.games, self.result["events"])

    def test_warning_does_not_evict_valid_existing_events(self):
        self.events = [dict(self.events[0], id=f"existing-{i}", title=f"Existing {i}") for i in range(5)]
        self.result.update(status="warning", message="Research incomplete",
                           events=[dict(self.events[0], id="new-event", title="New event", date="2026-10-10")])
        _, events, state, _ = self.apply()
        self.assertEqual(events, self.events)
        self.assertEqual(state["games"]["fictional-game"]["status"], "warning")

    def test_cancellation_requires_explicit_evidence(self):
        self.result["events"] = []
        cancellation = {"id": "fictional-release", "source_url": "https://example.com/cancelled", "reason": "Publisher cancellation"}
        self.result["cancelled_events"] = [cancellation]
        _, events, _, summary = self.apply()
        self.assertEqual(events, [])
        self.assertEqual(summary["events_removed"], 1)
        for key in ("source_url", "reason"):
            broken = dict(cancellation)
            broken.pop(key)
            self.result["cancelled_events"] = [broken]
            self.assertEqual(self.apply()[1], self.events)

    def test_ownership_guardrail(self):
        with self.assertRaises(ValueError):
            self.apply([dict(self.result, game_id="untracked")])
        with self.assertRaises(ValueError):
            apply_sync(self.games, self.events, self.state, {"games": [], "add": []}, NOW)
        with self.assertRaises(ValueError):
            self.apply(add=[dict(self.games[0], id="new-game", latest_news="")])
        with self.assertRaises(ValueError):
            self.apply([self.result, self.result])

    def test_mixed_multi_add_remove_and_targeting(self):
        self.second_game()
        additions = [dict(self.games[0], id=gid, name=gid) for gid in ("third-game", "fourth-game")]
        for g in additions:
            g.pop("latest_news")
        results = [dict(self.result, game_id=g["id"], events=[]) for g in additions]
        self.state["games"]["fictional-game"] = {"status": "failed", "last_attempt": NOW.isoformat()}
        games, events, state, summary = self.apply(results, add=additions, remove=["fictional-game"])
        self.assertEqual({g["id"] for g in games}, {"second-game", "third-game", "fourth-game"})
        self.assertEqual(events, [self.events[1]])
        self.assertEqual(games[0], self.games[1])
        self.assertNotIn("fictional-game", state["games"])
        self.assertNotIn("second-game", state["games"])
        self.assertEqual(summary["games_processed"], 2)
        self.assertEqual(summary["events_removed"], 1)
        self.assertTrue(all(g["latest_news"] == "New news." for g in games[1:]))

    def test_pure_multi_removal_and_repeat_noop(self):
        self.second_game()
        after = self.apply([], remove=["fictional-game", "second-game"])
        self.assertEqual(after[:2], ([], []))
        replay = apply_sync(*after[:3], {"games": []}, NOW, remove=["fictional-game", "second-game"])
        self.assertEqual(after[:3], replay[:3])

    def test_unknown_removal_preserves_production_order_and_bytes(self):
        before = read_store(ROOT / "data")
        after = apply_sync(*before, {"games": []}, NOW, remove=["untracked-game"])
        self.assertEqual(before, after[:3])
        self.assertEqual(after[3]["games_removed"], [])

    def test_documented_addition_example_and_legacy_removal(self):
        additions = json.loads((ROOT / "examples/add-games.json").read_text())
        proposal = json.loads((ROOT / "examples/research.json").read_text())
        after = apply_sync([], [], copy.deepcopy(DEFAULT_STATE), proposal, NOW, add=additions)
        self.assertEqual(after[3]["events_added"], 1)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            save_store(base, ([], [], copy.deepcopy(DEFAULT_STATE)), after[:3])
            command = [sys.executable, str(ROOT / "scripts/generate_calendar.py"),
                       "--games", str(base / "games.yaml"), "--events", str(base / "events.yaml"),
                       "--output", str(base / "feed.ics"), "--remove-game", "fictional-game"]
            subprocess.run(command, check=True, capture_output=True)
            self.assertEqual(read_store(base), ([], [], DEFAULT_STATE))

    def test_unchanged_replay_keeps_revisions_and_disk_bytes(self):
        after = self.apply()
        replay = apply_sync(*after[:3], {"games": [self.result]}, NOW)
        self.assertEqual(after[:3], replay[:3])
        self.assertEqual(replay[3]["events_updated"], 0)
        self.assertFalse(replay[3]["games"][0]["news_updated"])
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self.write_store(base)
            before = read_store(base)
            self.assertTrue(save_store(base, before, after[:3]))
            original = {p.name: p.read_bytes() for p in base.iterdir()}
            self.assertFalse(save_store(base, after[:3], replay[:3]))
            self.assertEqual(original, {p.name: p.read_bytes() for p in base.iterdir()})

    def test_retention_exact_end_boundary_all_day_and_timed(self):
        all_day = dict(self.events[0], date="2026-09-06")  # End: Sep 7 00:00 Prague
        end = instant(all_day, end=True)
        self.assertEqual(cleanup_old_events([all_day], end + timedelta(days=30)), [all_day])
        self.assertEqual(cleanup_old_events([all_day], end + timedelta(days=30, seconds=1)), [])
        timed = dict(self.events[0], start_at="2026-09-06T22:00:00Z", end_at="2026-09-07T22:00:00Z")
        timed.pop("date")
        end = instant(timed, end=True)
        self.assertEqual(cleanup_old_events([timed], end + timedelta(days=30)), [timed])
        self.assertEqual(cleanup_old_events([timed], end + timedelta(days=30, seconds=1)), [])

    def test_full_cleanup_even_when_research_failed(self):
        self.events[0]["date"] = "2026-01-01"
        result = {"game_id": "fictional-game", "status": "failed"}
        self.assertEqual(self.apply([result])[1], self.events)
        after = self.apply([result], full=True)
        self.assertEqual(after[1], [])
        self.assertEqual(after[3]["events_removed"], 1)
        self.assertEqual(after[3]["retention_removed"], 1)

    def test_read_only_queries_filters_and_chronology(self):
        self.second_game()
        self.events[1]["date"] = "2026-10-10"
        original = copy.deepcopy((self.games, self.events, self.state))
        result = upcoming_events(self.games, self.events, NOW, 30)
        self.assertEqual([e["id"] for e in result], ["second-release", "fictional-release"])
        self.assertEqual(list_games(self.games, self.events, self.state, NOW)[0]["future_events"], 1)
        self.assertEqual(original, (self.games, self.events, self.state))
        self.events[1]["status"] = "tentative"
        self.assertEqual(len(upcoming_events(self.games, self.events, NOW, 30)), 1)
        with self.assertRaises(ValueError):
            upcoming_events(self.games, self.events, NOW, -1)

    def test_news_clear_advances_calendar_revision(self):
        self.games[0].update(latest_news_updated_at="2026-10-01T12:00:00Z")
        self.result["latest_news"] = ""
        self.result["latest_news_source_url"] = ""
        games, events, _, _ = self.apply()
        text = generate(games, events)
        self.assertIn("LAST-MODIFIED:20261007T120000Z", text)
        self.assertNotIn("Latest news:", text)

    def write_store(self, base):
        (base / "games.yaml").write_text(yaml.safe_dump({"games": self.games}))
        (base / "events.yaml").write_text(yaml.safe_dump({"events": self.events}))
        (base / "sync.yaml").write_text(yaml.safe_dump(self.state))

    def test_interrupted_transaction_can_recover(self):
        after = self.apply()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self.write_store(base)
            with patch("scripts.sync_calendar.recover", side_effect=OSError("Interruption")):
                with self.assertRaises(OSError):
                    save_store(base, (self.games, self.events, self.state), after[:3])
            with self.assertRaisesRegex(ValueError, "Interrupted"):
                read_store(base)
            recover(base)
            self.assertEqual(read_store(base), after[:3])
            recover(base)
            self.assertFalse((base / ".sync-transaction.json").exists())

    def test_invalid_recovery_does_not_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self.write_store(base)
            original = (base / "games.yaml").read_bytes()
            journal = base / ".sync-transaction.json"
            journal.write_text(json.dumps({"games.yaml": "games: []\n"}))
            with self.assertRaises(ValueError):
                recover(base)
            self.assertEqual((base / "games.yaml").read_bytes(), original)
            journal.write_text(json.dumps({"../outside": "no"}))
            with self.assertRaises(ValueError):
                recover(base)

    def test_cli_readonly_dryrun_and_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            self.write_store(base)
            proposal = base / "proposal.json"
            proposal.write_text(json.dumps({"games": [self.result]}))
            command = [sys.executable, str(ROOT / "scripts/sync_calendar.py"), "--data-dir", str(base), "--now", NOW.isoformat()]
            original = {p.name: p.read_bytes() for p in base.iterdir()}
            for args in (["list"], ["upcoming", "--days", "30"], ["validate"],
                         ["apply", "--proposal", str(proposal), "--dry-run"]):
                subprocess.run(command + args, check=True, capture_output=True)
                self.assertEqual(original, {p.name: p.read_bytes() for p in base.iterdir()})
            first = subprocess.run(command + ["apply", "--proposal", str(proposal)], check=True, capture_output=True, text=True)
            self.assertTrue(json.loads(first.stdout)["changed"])
            second = subprocess.run(command + ["apply", "--proposal", str(proposal)], check=True, capture_output=True, text=True)
            self.assertFalse(json.loads(second.stdout)["changed"])
            subprocess.run(command + ["apply", "--remove", "fictional-game"], check=True, capture_output=True)
            self.assertEqual(read_store(base), ([], [], self.state))


if __name__ == "__main__":
    unittest.main()
