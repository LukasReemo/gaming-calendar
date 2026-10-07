"""Deterministic state operations. Research is supplied by Codex, never performed here."""
import argparse
import copy
import fcntl
from datetime import datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfoNotFoundError

import yaml

if __package__:
    from .generate_calendar import (ROOT, TYPES, PRAGUE, atomic_write, event_timing,
                                    load_rows, required_text, timestamp, url, validate)
else:
    from generate_calendar import (ROOT, TYPES, PRAGUE, atomic_write, event_timing,
                                   load_rows, required_text, timestamp, url, validate)

ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
DEFAULT_STATE = {"version": 1, "past_event_retention_days": 30, "games": {}}
GAME_FIELDS = {"id", "name", "tracking_status", "tracked_event_types", "latest_news",
               "latest_news_source_url", "latest_news_updated_at"}
EVENT_FIELDS = {"id", "logical_key", "game_id", "title", "type", "date", "start_at", "end_at",
                "source_timezone", "status", "source_url", "notes", "updated_at", "importance"}
RESULT_FIELDS = {"game_id", "status", "message", "events", "cancelled_events", "latest_news",
                 "latest_news_source_url"}


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError(f"Invalid stable ID: {value!r}")
    return value


def fields(value, allowed, context):
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError(f"Invalid {context} fields; allowed: {', '.join(sorted(allowed))}")


def rows(value, context):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"{context} must be a list of mappings")
    return value


def instant(event, end=False):
    kind, start, finish = event_timing(event)
    value = finish if end else start
    if kind == "all_day":
        value = datetime.combine(value, time.min, PRAGUE)
    return value.astimezone(timezone.utc)


def validate_state(games, events, state):
    # load_rows validates IDs on disk; enforce the same rules for in-memory proposals.
    for collection in (games, events):
        ids = [identifier(required_text(row, "id")) for row in collection]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate stable IDs")
    validate(games, events)
    fields(state, {"version", "past_event_retention_days", "games"}, "sync state")
    if type(state.get("version")) is not int or state["version"] != 1 or type(state.get("past_event_retention_days")) is not int or state["past_event_retention_days"] < 0:
        raise ValueError("Sync state requires version 1 and nonnegative integer retention days")
    if not isinstance(state.get("games"), dict):
        raise ValueError("Sync state games must be a mapping")
    tracked = {g["id"] for g in games}
    if set(state["games"]) - tracked:
        raise ValueError("Sync metadata references untracked games")
    for meta in state["games"].values():
        fields(meta, {"last_attempt", "last_success", "status", "message"}, "sync metadata")
        if meta.get("status") not in {"OK", "warning", "failed"}:
            raise ValueError("Invalid sync status")
        timestamp(required_text(meta, "last_attempt"))
        if "last_success" in meta:
            if timestamp(meta["last_success"]) > timestamp(meta["last_attempt"]):
                raise ValueError("Successful sync cannot be later than attempt")
        if not isinstance(meta.get("message", ""), str):
            raise ValueError("Sync message must be text")
    keys = set()
    for event in events:
        fields(event, EVENT_FIELDS, "event")
        if type(event.get("importance", 0)) is not int or not 0 <= event.get("importance", 0) <= 3:
            raise ValueError("importance must be an integer from 0 to 3")
        if "logical_key" in event:
            key = (event["game_id"], identifier(event["logical_key"]))
            if key in keys:
                raise ValueError("Duplicate logical event key")
            keys.add(key)


def cleanup_old_events(events, now, retention_days=30):
    cutoff = now - timedelta(days=retention_days)
    return [e for e in events if instant(e, end=True) >= cutoff]


def select_events(events, now, protected=()):
    """Future first, nearest date, then importance (0..3) and stable ID.

    During incomplete research existing IDs are protected against capacity eviction.
    """
    priority = lambda e: (e["id"] not in protected, instant(e) < now,
                          instant(e) if instant(e) >= now else -instant(e).timestamp(),
                          -e.get("importance", 0), e["id"])
    return sorted(events, key=priority)[:5]


def list_games(games, events, state, now):
    return [{"id": g["id"], "name": g["name"],
             "future_events": sum(e["game_id"] == g["id"] and instant(e) >= now and
                                  e["status"] in {"confirmed", "expected"} and
                                  e["type"] in g["tracked_event_types"] for e in events),
             "last_attempt": state["games"].get(g["id"], {}).get("last_attempt"),
             "last_success": state["games"].get(g["id"], {}).get("last_success"),
             "status": state["games"].get(g["id"], {}).get("status", "warning"),
             "message": state["games"].get(g["id"], {}).get("message", "Not yet synced")}
            for g in games]


def upcoming_events(games, events, now, days=30):
    if type(days) is not int or days < 0:
        raise ValueError("Window days must be a nonnegative integer")
    indexed = {g["id"]: g for g in games}
    return sorted([dict(e, game_name=indexed[e["game_id"]]["name"])
                   for e in events if now <= instant(e) < now + timedelta(days=days)
                   and e["status"] in {"confirmed", "expected"}
                   and e["type"] in indexed[e["game_id"]]["tracked_event_types"]],
                  key=lambda e: (instant(e), e["id"]))


def reconcile_game(game, existing, result, now):
    fields(result, RESULT_FIELDS, "research result")
    if result.get("status") not in {"OK", "warning", "failed"}:
        raise ValueError("Research status must be OK, warning, or failed")
    if not isinstance(result.get("message", ""), str):
        raise ValueError("Research message must be text")
    if result["status"] == "failed":
        if set(result) - {"game_id", "status", "message"}:
            raise ValueError("Failed research cannot supply replacement data")
        return game, existing
    if "latest_news" not in result:
        raise ValueError("Successful/incomplete research must supply latest_news")
    proposed = copy.deepcopy(rows(result.get("events", []), "events"))
    merged = {e["id"]: copy.deepcopy(e) for e in existing}
    old_by_key = {e.get("logical_key", e["id"]): e for e in existing}
    seen = set()
    for event in proposed:
        fields(event, EVENT_FIELDS, "proposed event")
        key = identifier(event["logical_key"]) if "logical_key" in event else None
        old = old_by_key.get(key) if key else None
        if "id" in event:
            eid = identifier(event["id"])
            if old and old["id"] != eid:
                raise ValueError("Logical key must retain its existing event ID")
        elif key:
            eid = old["id"] if old else game["id"] + "-" + key
        else:
            raise ValueError("Events require an existing id or a permanent logical_key")
        if eid in seen:
            raise ValueError("Duplicate proposed event")
        seen.add(eid)
        if event.get("game_id", game["id"]) != game["id"]:
            raise ValueError("Research event belongs to another game")
        event.update(id=eid, game_id=game["id"])
        if event.get("status") == "expected":
            raise ValueError("New research must use confirmed or tentative, not legacy expected")
        old = merged.get(eid)
        # Catch common accidental duplicate identities, including date/time corrections.
        signature = (required_text(event, "title").strip().casefold(), event.get("type"))
        if any(e["id"] != eid and (e["title"].strip().casefold(), e["type"]) == signature
               for e in merged.values()):
            raise ValueError("Duplicate logical title/type; reuse the stored ID")
        if old and "logical_key" in old:
            if event.get("logical_key", old["logical_key"]) != old["logical_key"]:
                raise ValueError("A logical key is permanent")
            event["logical_key"] = old["logical_key"]
        event.pop("updated_at", None)
        previous = {k: v for k, v in (old or {}).items() if k != "updated_at"}
        event["updated_at"] = old["updated_at"] if old and previous == event else now.isoformat()
        validate([game], [event])
        merged[eid] = event
    cancelled = rows(result.get("cancelled_events", []), "cancelled_events")
    seen_cancelled = set()
    for cancellation in cancelled:
        fields(cancellation, {"id", "source_url", "reason"}, "cancellation")
        eid = identifier(required_text(cancellation, "id"))
        url(required_text(cancellation, "source_url"))
        required_text(cancellation, "reason")
        if eid in seen or eid in seen_cancelled:
            raise ValueError("Conflicting or duplicate cancellation")
        seen_cancelled.add(eid)
        if eid not in merged:
            raise ValueError("Cancellation must reference an existing event of this game")
        del merged[eid]
    updated_game = copy.deepcopy(game)
    news = result["latest_news"]
    source = result.get("latest_news_source_url", "")
    if news != game.get("latest_news", "") or source != game.get("latest_news_source_url", ""):
        updated_game.update(latest_news=news, latest_news_source_url=source,
                            latest_news_updated_at=now.isoformat())
    # Even unchanged supplied news must pass validation (including null/type errors).
    validate([dict(updated_game, latest_news=news, latest_news_source_url=source)],
             list(merged.values()), enforce_limit=False)
    protected = {e["id"] for e in existing} if result["status"] == "warning" else set()
    return updated_game, select_events(list(merged.values()), now, protected)


def apply_sync(games, events, state, proposal, now, *, full=False, add=(), remove=()):
    """Pure transaction: explicit ownership changes are separate from research input."""
    validate_state(games, events, state)
    if now.tzinfo is None:
        raise ValueError("now must be timezone aware")
    fields(proposal, {"games"}, "proposal")
    results = rows(proposal.get("games", []), "proposal games")
    result_ids = [identifier(required_text(r, "game_id")) for r in results]
    if len(result_ids) != len(set(result_ids)):
        raise ValueError("Duplicate game result")
    input_order = {e["id"]: i for i, e in enumerate(events)}
    games, events, state = copy.deepcopy((games, events, state))
    additions = rows(list(add), "add games")
    removed = {identifier(gid) for gid in remove}
    removed_event_count = sum(e["game_id"] in removed for e in events)
    if removed & {g.get("id") for g in additions}:
        raise ValueError("Cannot add and remove the same game")
    tracked = {g["id"] for g in games}
    actual_removed = removed & tracked
    for game in additions:
        fields(game, GAME_FIELDS, "added game")
        gid = identifier(required_text(game, "id"))
        if gid in tracked:
            raise ValueError(f"Already tracked: {gid}")
        if gid not in result_ids:
            raise ValueError("New games require an immediate research result")
        if set(game) & {"latest_news", "latest_news_source_url", "latest_news_updated_at"}:
            raise ValueError("Supply new-game news through research, not tracking configuration")
        validate([game], [])
        games.append(game)
        tracked.add(gid)
    games = [g for g in games if g["id"] not in removed]
    events = [e for e in events if e["game_id"] not in removed]
    state["games"] = {k: v for k, v in state["games"].items() if k not in removed}
    tracked = {g["id"] for g in games}
    if set(result_ids) - tracked:
        raise ValueError("Cannot sync untracked games (explicit add is required)")
    by_result = dict(zip(result_ids, results))
    summary = []
    for i, game in enumerate(games):
        gid = game["id"]
        result = by_result.get(gid)
        if result is None and not full:
            continue
        if result is None:
            result = {"game_id": gid, "status": "failed", "message": "Missing full-sync research result"}
        existing = [e for e in events if e["game_id"] == gid]
        before = {e["id"]: e for e in existing}
        try:
            new_game, new_events = reconcile_game(game, existing, result, now)
            other_ids = {e["id"] for e in events if e["game_id"] != gid}
            if any(e["id"] in other_ids for e in new_events):
                raise ValueError("Event ID is already owned by another game")
            # Validate each game's complete proposed state before accepting it.
            validate_state([new_game], new_events, dict(DEFAULT_STATE, games={}))
            status, message = result["status"], result.get("message", "")
        except (ValueError, TypeError, KeyError, OverflowError, ZoneInfoNotFoundError) as error:
            new_game, new_events = game, existing
            status, message = "failed", str(error)
        games[i] = new_game
        events = [e for e in events if e["game_id"] != gid] + new_events
        summary.append({"game_id": gid, "status": status, "message": message,
                        "attempted_at": now.isoformat(),
                        "news_updated": new_game != game, "before": before})
    before_cleanup = len(events)
    if full:
        events = cleanup_old_events(events, now, state["past_event_retention_days"])
    # Keep v1 order and no-op operations byte-stable; append only new records.
    events.sort(key=lambda e: (input_order.get(e["id"], len(input_order)), e["id"]))
    for item in summary:
        before = item.pop("before")
        after = {e["id"]: e for e in events if e["game_id"] == item["game_id"]}
        item.update(added=len(after.keys() - before.keys()), removed=len(before.keys() - after.keys()),
                    updated=sum(after[k] != before[k] for k in after.keys() & before.keys()))
        # Timestamp-only attempts belong in the run summary, not Git history.
        # Persist first observations, content changes and status/error transitions.
        gid = item["game_id"]
        previous = state["games"].get(gid)
        checkpoint = (previous is None or item["news_updated"] or before != after or
                      previous.get("status") != item["status"] or
                      previous.get("message", "") != item["message"])
        item["checkpoint_updated"] = checkpoint
        if checkpoint:
            metadata = dict(previous or {}, last_attempt=now.isoformat(),
                            status=item["status"], message=item["message"])
            if item["status"] == "OK":
                metadata["last_success"] = now.isoformat()
            state["games"][gid] = metadata
    validate_state(games, events, state)
    return games, events, state, {"games": summary, "games_processed": len(summary),
                                "events_added": sum(i["added"] for i in summary),
                                "events_updated": sum(i["updated"] for i in summary),
                                "events_removed": removed_event_count + sum(i["removed"] for i in summary),
                                "failed_games": sum(i["status"] == "failed" for i in summary),
                                "retention_removed": before_cleanup - len(events),
                                "games_added": [g["id"] for g in additions], "games_removed": sorted(actual_removed)}


def read_store(directory):
    if (directory / ".sync-transaction.json").exists():
        raise ValueError("Interrupted state write; run recover before reading")
    games = load_rows(directory / "games.yaml", "games")
    events = load_rows(directory / "events.yaml", "events")
    path = directory / "sync.yaml"
    state = yaml.safe_load(path.read_text()) if path.exists() else copy.deepcopy(DEFAULT_STATE)
    validate_state(games, events, state)
    return games, events, state


def recover(directory):
    """Complete a validated journal after an interrupted multi-file replacement."""
    journal = directory / ".sync-transaction.json"
    if journal.exists():
        contents = json.loads(journal.read_text())
        if not isinstance(contents, dict) or set(contents) - {"games.yaml", "events.yaml", "sync.yaml"}:
            raise ValueError("Invalid transaction journal")
        def restored(name):
            content = contents.get(name)
            if content is not None and not isinstance(content, str):
                raise ValueError("Invalid transaction content")
            return yaml.safe_load(content if content is not None else (directory / name).read_text())
        restored_games, restored_events = restored("games.yaml"), restored("events.yaml")
        if not isinstance(restored_games, dict) or set(restored_games) != {"games"} or not isinstance(restored_events, dict) or set(restored_events) != {"events"}:
            raise ValueError("Invalid transaction state")
        restored_state = restored("sync.yaml") if "sync.yaml" in contents or (directory / "sync.yaml").exists() else copy.deepcopy(DEFAULT_STATE)
        validate_state(rows(restored_games["games"], "games"), rows(restored_events["events"], "events"), restored_state)
        for name, content in contents.items():
            if not isinstance(content, str):
                raise ValueError("Invalid transaction content")
            atomic_write(directory / name, content.encode())
        journal.unlink()


def save_store(directory, before, after):
    contents = {}
    for name, key, old, new in zip(("games.yaml", "events.yaml", "sync.yaml"),
                                  ("games", "events", None), before, after):
        if old != new:
            contents[name] = yaml.safe_dump({key: new} if key else new, allow_unicode=True, sort_keys=False)
    if contents:
        atomic_write(directory / ".sync-transaction.json", json.dumps(contents).encode())
        recover(directory)
    return bool(contents)


def run(args, parser):
    try:
        now = timestamp(args.now) if args.now else datetime.now(timezone.utc)
        if args.command == "recover":
            recover(args.data_dir)
            read_store(args.data_dir)
            print("Recovered and validated state")
            return
        before = read_store(args.data_dir)
        games, events, state = before
        if args.command == "validate":
            output = {"valid": True, "games": len(games), "events": len(events)}
        elif args.command == "list":
            output = list_games(games, events, state, now)
        elif args.command == "upcoming":
            output = upcoming_events(games, events, now, args.days)
        elif args.command == "cleanup":
            events = cleanup_old_events(events, now, state["past_event_retention_days"])
            output = {"removed": len(before[1]) - len(events),
                      "changed": save_store(args.data_dir, before, (games, events, state))}
        else:
            proposal = json.loads(args.proposal.read_text()) if args.proposal else {"games": []}
            add = json.loads(args.add.read_text()) if args.add else []
            games, events, state, output = apply_sync(*before, proposal, now, full=args.full, add=add, remove=args.remove)
            output["changed"] = before != (games, events, state)
            output["content_changed"] = before[:2] != (games, events)
            if not args.dry_run:
                save_store(args.data_dir, before, (games, events, state))
        print(json.dumps(output, indent=2, default=str, ensure_ascii=False))
    except (ValueError, TypeError, KeyError, OverflowError, OSError, ZoneInfoNotFoundError, yaml.YAMLError) as error:
        parser.error(str(error))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--now", help="Timezone-aware ISO timestamp for reproducible runs")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "list", "recover", "cleanup"):
        commands.add_parser(name)
    upcoming = commands.add_parser("upcoming")
    upcoming.add_argument("--days", type=int, default=30)
    apply = commands.add_parser("apply")
    apply.add_argument("--proposal", type=Path, help="JSON research envelope; defaults to no research")
    apply.add_argument("--full", action="store_true")
    apply.add_argument("--add", type=Path, help="Explicit user-requested new game mappings (JSON list)")
    apply.add_argument("--remove", nargs="+", default=[], help="Explicit user-requested game IDs")
    apply.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        # Lock the directory itself: read-only commands create no lock/state files.
        descriptor = os.open(args.data_dir, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH if args.command in {"validate", "list", "upcoming"} or
                        (args.command == "apply" and args.dry_run) else fcntl.LOCK_EX)
            run(args, parser)
        finally:
            os.close(descriptor)
    except OSError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
