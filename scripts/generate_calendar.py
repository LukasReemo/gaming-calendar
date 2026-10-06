"""Validate repository data and generate a deterministic iCalendar feed."""
import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import os
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
TYPES = {"release": "🎮", "expansion": "🌍", "dlc": "🧩", "season": "🌑",
         "major_update": "🔄", "beta": "🧪", "early_access": "🚀"}
STATUSES = {"confirmed", "expected", "rumored", "delayed"}


def required_text(row, field):
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be nonempty text: {row.get('id', '?')}")
    return value


def optional_text(row, field):
    value = row.get(field, "")
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text (omit it when absent)")
    return value.strip()


def url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or any(c.isspace() for c in value):
        raise ValueError(f"Invalid HTTP(S) source URL: {value!r}")


def day(value):
    if isinstance(value, datetime):
        raise ValueError("Use a date without a time")
    return date.fromisoformat(str(value))


def timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("updated_at must include a timezone")
    return parsed.astimezone(timezone.utc)


PRAGUE = ZoneInfo("Europe/Prague")


def source_datetime(value, zone_name=None):
    """Resolve an explicit source time; reject ambiguous/nonexistent wall times."""
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", value):
        raise ValueError("start_at/end_at must be quoted ISO date-times with an exact time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    zone = ZoneInfo(zone_name) if zone_name else None
    if parsed.tzinfo is not None:
        if zone and parsed.astimezone(zone).replace(tzinfo=None) != parsed.replace(tzinfo=None):
            raise ValueError("Source offset does not match source_timezone")
        return parsed.astimezone(PRAGUE)
    if zone is None:
        raise ValueError("A local source time requires source_timezone")
    candidates = set()
    for fold_value in (0, 1):
        candidate = parsed.replace(tzinfo=zone, fold=fold_value).astimezone(timezone.utc)
        if candidate.astimezone(zone).replace(tzinfo=None) == parsed:
            candidates.add(candidate)
    if len(candidates) != 1:
        raise ValueError("Ambiguous or nonexistent source time: supply a valid explicit offset")
    return candidates.pop().astimezone(PRAGUE)


def event_timing(event):
    """Canonical timing shared by validation, output, and future reconciliation."""
    if "start_at" not in event:
        if "end_at" in event or "source_timezone" in event:
            raise ValueError("Timing metadata requires start_at")
        start = day(event.get("date"))
        return "all_day", start, start + timedelta(days=1)
    if "date" in event:
        raise ValueError("Use either date or start_at, never both")
    zone_name = event.get("source_timezone")
    if zone_name is not None and (not isinstance(zone_name, str) or not zone_name):
        raise ValueError("source_timezone must be an IANA timezone")
    start = source_datetime(event["start_at"], zone_name)
    end = (source_datetime(event["end_at"], zone_name) if "end_at" in event else
           (start.astimezone(timezone.utc) + timedelta(hours=1)).astimezone(PRAGUE))
    if end.astimezone(timezone.utc) <= start.astimezone(timezone.utc):
        raise ValueError("end_at must be later than start_at")
    return "timed", start, end


def calendar_datetime(field, value):
    # RFC 5545 local times select the first occurrence during a clock rollback.
    # UTC preserves the second occurrence without changing the actual instant.
    if value.fold == 1:
        return f"{field}:{value.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
    return f"{field};TZID=Europe/Prague:{value:%Y%m%dT%H%M%S}"


def prague_timezone(years):
    """Embed actual IANA transitions, including a baseline, for portable ICS clients."""
    first, last = min(years) - 1, max(years) + 1
    cursor = datetime(max(1, first), 1, 1, tzinfo=timezone.utc)
    stop = datetime(min(9999, last + 1), 1, 1, tzinfo=timezone.utc)
    previous = cursor.astimezone(PRAGUE)
    lines = ["BEGIN:VTIMEZONE", "TZID:Europe/Prague", "X-LIC-LOCATION:Europe/Prague"]
    def offset(value):
        minutes = int(value.total_seconds() / 60)
        return ("+" if minutes >= 0 else "-") + f"{abs(minutes)//60:02}{abs(minutes)%60:02}"
    def observance(instant, before, after):
        kind = "DAYLIGHT" if after.dst() else "STANDARD"
        wall = instant.replace(tzinfo=None) + before.utcoffset()
        return [f"BEGIN:{kind}", f"DTSTART:{wall:%Y%m%dT%H%M%S}",
                "TZOFFSETFROM:" + offset(before.utcoffset()),
                "TZOFFSETTO:" + offset(after.utcoffset()), "TZNAME:" + after.tzname(),
                f"END:{kind}"]
    lines.extend(observance(cursor, previous, previous))
    while cursor < stop:
        cursor += timedelta(hours=1)
        current = cursor.astimezone(PRAGUE)
        if current.utcoffset() != previous.utcoffset():
            lines.extend(observance(cursor, previous, current))
        previous = current
    return lines + ["END:VTIMEZONE"]


def remove_tracked_game(games, events, game_id):
    """Only remove source events owned by the exact stable game ID; repeat safely."""
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", game_id):
        raise ValueError("Invalid game ID")
    return ([g for g in games if g["id"] != game_id],
            [e for e in events if e["game_id"] != game_id])


def check_managed_feed(path):
    """Accept our dedicated feed, including legacy UIDs; refuse mixed/manual output."""
    if not Path(path).exists():
        return
    text = Path(path).read_text(encoding="utf-8").replace("\n ", "").replace("\n\t", "")
    if "PRODID:-//Gaming Calendar//EN" not in text.splitlines():
        raise ValueError("Cleanup output must be a Gaming Calendar feed")
    for block in text.split("BEGIN:VEVENT")[1:]:
        lines = block.split("END:VEVENT", 1)[0].splitlines()
        uids = [line[4:] for line in lines if line.startswith("UID:")]
        if len(uids) != 1 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*@gaming-calendar", uids[0]):
            raise ValueError("Cleanup refuses a feed containing unrelated events")


def atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_rows(path, key):
    with Path(path).open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict) or set(data) != {key} or not isinstance(data[key], list):
        raise ValueError(f"{path} must contain a '{key}' list")
    seen = set()
    for row in data[key]:
        if not isinstance(row, dict):
            raise ValueError(f"{key} entries must be mappings")
        identifier = required_text(row, "id")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", identifier) or identifier in seen:
            raise ValueError(f"Invalid or duplicate ID: {identifier}")
        seen.add(identifier)
    return data[key]


def validate(games, events):
    indexed = {g["id"]: g for g in games}
    for game in games:
        required_text(game, "name")
        if game.get("tracking_status") not in {"playing", "interested", "waiting"}:
            raise ValueError("Invalid tracking_status")
        tracked = game.get("tracked_event_types")
        if not isinstance(tracked, list) or any(t not in TYPES for t in tracked):
            raise ValueError("Invalid tracked_event_types")
        news = optional_text(game, "latest_news")
        # Simple editorial rule: punctuation followed by whitespace ends a sentence.
        if len(re.split(r"[.!?]+(?:\s+|$)", news.strip())) - (1 if re.search(r"[.!?]$", news) else 0) > 3:
            raise ValueError("latest_news must contain at most three sentences")
        source = optional_text(game, "latest_news_source_url")
        if source:
            url(source)
        if "latest_news_updated_at" in game:
            timestamp(game["latest_news_updated_at"])
    for event in events:
        required_text(event, "title")
        if event.get("game_id") not in indexed:
            raise ValueError("Event references an unknown game")
        if event.get("type") not in TYPES or event.get("status") not in STATUSES:
            raise ValueError("Invalid event type or status")
        event_timing(event)
        url(required_text(event, "source_url"))
        optional_text(event, "notes")
        timestamp(required_text(event, "updated_at"))
    return indexed


def escape(value):
    return value.replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def fold(line):
    """Fold at 75 UTF-8 octets, without splitting a Unicode code point."""
    parts, current = [], ""
    for character in line:
        if len((current + character).encode("utf-8")) > 75:
            parts.append(current)
            current = " "
        current += character
    parts.append(current)
    return "\r\n".join(parts)


def generate(games, events):
    indexed = validate(games, events)
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Gaming Calendar//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Gaming Calendar", "X-WR-TIMEZONE:Europe/Prague"]
    timings = {e["id"]: event_timing(e) for e in events}
    years = [v.year for kind, start, end in timings.values() if kind == "timed" for v in (start, end)]
    if years:
        lines.extend(prague_timezone(years))
    for event in sorted(events, key=lambda e: e["id"]):
        game = indexed[event["game_id"]]
        if event["status"] not in {"confirmed", "expected"} or event["type"] not in game["tracked_event_types"]:
            continue
        description = [f"Game: {game['name']}", f"Type: {event['type'].replace('_', ' ').title()}",
                       f"Status: {event['status'].title()}"]
        if optional_text(event, "notes"):
            description.extend(["", event["notes"]])
        news = optional_text(game, "latest_news")
        modified = timestamp(event["updated_at"])
        if news:
            description.extend(["", "Latest news:", news])
            if optional_text(game, "latest_news_source_url"):
                description.extend(["", "News source:", game["latest_news_source_url"]])
            if "latest_news_updated_at" in game:
                modified = max(modified, timestamp(game["latest_news_updated_at"]))
        description.extend(["", "Event source:", event["source_url"]])
        stamp = modified.strftime("%Y%m%dT%H%M%SZ")
        kind, start, end = timings[event["id"]]
        if kind == "timed":
            description.extend(["", "Source start: " + event["start_at"]])
            if "source_timezone" in event:
                description.append("Source timezone: " + event["source_timezone"])
            if "end_at" in event:
                description.append("Source end: " + event["end_at"])
        timing_lines = ([f"DTSTART;VALUE=DATE:{start:%Y%m%d}", f"DTEND;VALUE=DATE:{end:%Y%m%d}"]
                        if kind == "all_day" else
                        [calendar_datetime("DTSTART", start), calendar_datetime("DTEND", end)])
        lines.extend(["BEGIN:VEVENT", f"UID:{event['id']}@gaming-calendar",
                      f"DTSTAMP:{stamp}", f"LAST-MODIFIED:{stamp}",
                      "X-GAMING-CALENDAR-MANAGED:TRUE",
                      f"X-GAMING-CALENDAR-GAME-ID:{event['game_id']}",
                      f"X-GAMING-CALENDAR-EVENT-ID:{event['id']}",
                      *timing_lines,
                      "SUMMARY:" + escape(f"{TYPES[event['type']]} {game['name']} · {event['title']}"),
                      "DESCRIPTION:" + escape("\n".join(description)),
                      "STATUS:" + ("CONFIRMED" if event["status"] == "confirmed" else "TENTATIVE"),
                      "TRANSP:TRANSPARENT", "END:VEVENT"])
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=Path, default=ROOT / "data/games.yaml")
    parser.add_argument("--events", type=Path, default=ROOT / "data/events.yaml")
    parser.add_argument("--output", type=Path, default=ROOT / "public/gaming-calendar.ics")
    parser.add_argument("--remove-game", help="Remove a stable game ID and its managed events")
    args = parser.parse_args()
    try:
        games, events = load_rows(args.games, "games"), load_rows(args.events, "events")
        if args.remove_game:
            check_managed_feed(args.output)
            games, events = remove_tracked_game(games, events, args.remove_game)
        calendar = generate(games, events)
    except (ValueError, TypeError, OverflowError, ZoneInfoNotFoundError, yaml.YAMLError) as error:
        parser.error(str(error))
    # Publish cleanup first, then remove event rows, then game rows. A retry is safe
    # after an interrupted write and never loses the ownership needed for cleanup.
    atomic_write(args.output, calendar.encode("utf-8"))
    if args.remove_game:
        atomic_write(args.events, yaml.safe_dump({"events": events}, allow_unicode=True, sort_keys=False).encode("utf-8"))
        atomic_write(args.games, yaml.safe_dump({"games": games}, allow_unicode=True, sort_keys=False).encode("utf-8"))
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
