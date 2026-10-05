"""Validate repository data and generate a deterministic all-day iCalendar feed."""
import argparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
from urllib.parse import urlsplit

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
        event_day = day(event.get("date"))
        if event_day == date.max:
            raise ValueError("Event date cannot be 9999-12-31")
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
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:Gaming Calendar"]
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
        start = day(event["date"])
        lines.extend(["BEGIN:VEVENT", f"UID:{event['id']}@gaming-calendar",
                      f"DTSTAMP:{stamp}", f"LAST-MODIFIED:{stamp}",
                      f"DTSTART;VALUE=DATE:{start:%Y%m%d}",
                      f"DTEND;VALUE=DATE:{start + timedelta(days=1):%Y%m%d}",
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
    args = parser.parse_args()
    try:
        calendar = generate(load_rows(args.games, "games"), load_rows(args.events, "events"))
    except (ValueError, TypeError, yaml.YAMLError) as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(calendar.encode("utf-8"))
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
