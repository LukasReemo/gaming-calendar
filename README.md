# Gaming Calendar

A personal, all-day iCalendar feed for Apple Calendar. Git-tracked YAML is the source of truth; Python generates the feed, and GitHub Actions publishes it to GitHub Pages. There is no database, server, frontend, or research automation.

The tracked games are Diablo IV, Path of Exile 2, Gray Zone Warfare, Borderlands 4, and Crimson Desert. Their news fields and the production event list are deliberately empty: no verified events or news were supplied. All test events are fictional.

## Architecture and files

```text
data/games.yaml                         tracked games and current game-level news
data/events.yaml                        actual calendar events
scripts/generate_calendar.py            validation and iCalendar generation
public/gaming-calendar.ics              generated local preview and Pages feed
tests/test_calendar.py                  standard-library unittest tests
.github/workflows/generate-calendar.yml  validate, generate, publish
requirements.txt                        single pinned dependency: PyYAML
```

PyYAML safely parses normal YAML, avoiding an incomplete handwritten parser. Everything else uses Python's standard library. Output is UTF-8 with CRLF line endings, iCalendar text escaping, and folding at 75 bytes without splitting Unicode characters.

Pages deploys the generated `public/` directory as an artifact. The workflow never commits output back into Git, avoiding bot commits and write permissions on repository contents. The included `.ics` is a local preview; each deployment regenerates it from source. You do not need to commit subsequent generated output changes.

## Run locally

Use Python 3.12 or newer from the repository root:

```sh
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/generate_calendar.py
```

The generator resolves default paths relative to its own location, so it also works from another directory. Optional `--games`, `--events`, and `--output` arguments select alternative files. Invalid input fails generation rather than publishing a partial feed. Tests validate YAML, required fields, stable identity, filtering, news injection, escaping, and byte-length folding.

## Manage tracked games

Add a mapping under `games:` or edit an existing one:

```yaml
# Fictional example only; do not copy as real game news.
- id: fictional-game
  name: Fictional Game
  tracking_status: interested
  tracked_event_types: [release, dlc, major_update]
  latest_news: ""
```

IDs must be unique lowercase letters/digits separated by hyphens. Keep IDs permanent even when a name changes. `tracking_status` is personal metadata: `playing`, `interested`, or `waiting`; it does not filter events. `tracked_event_types` controls export for that game. Use an empty list to pause its events. Supported types are `release`, `expansion`, `dlc`, `season`, `major_update`, `beta`, and `early_access`.

To remove a game, remove its mapping and every event referencing its ID. Unknown game references fail validation. To change tracking, edit its event-type list; existing YAML events remain available for later re-enabling.

## Update latest news

Edit the game's `latest_news`, not individual events. Keep it to three short sentences or fewer. Optionally add `latest_news_source_url` (HTTP/HTTPS) and `latest_news_updated_at` (a quoted ISO 8601 timestamp with timezone, such as `"2026-10-05T12:00:00Z"`). Update that timestamp whenever news or its source changes; the generator uses it for exported event revision timestamps.

The sentence check treats `.`, `!`, or `?` followed by whitespace or end of text as a boundary; abbreviations may count as sentences. Prefer simple prose. Leave news as `""` or omit the field when nothing is verified. Omit optional fields rather than setting them to YAML `null`.

Every exported event for the game receives the current summary and optional source. When news is absent, both sections are omitted. Clear stale news and update affected event `updated_at` timestamps to record its removal. Future automation can edit these same fields without changing the generator.

## Add, edit, or remove events

Add mappings under `events:`. The following is **fictional test data**, not production information:

```yaml
events:
  - id: fictional-game-release
    game_id: fictional-game
    title: Release
    type: release
    date: '2030-01-01'
    status: confirmed
    source_url: https://example.com/announcement
    updated_at: '2026-10-05T12:00:00Z'
    notes: |
      Optional notes about this fictional event.
```

All shown fields except `notes` are required. Dates are ISO `YYYY-MM-DD`, without a time. `updated_at` is a timezone-aware ISO timestamp: advance it whenever the event's date, title, status, source, or notes change. When changing a game name, tracking, or removing news, also advance affected event timestamps. Keep the event ID unchanged when its date changes. A truly distinct event needs a new ID; never reuse deleted IDs for unrelated events.

Statuses are `confirmed`, `expected`, `rumored`, and `delayed`. Only confirmed and expected events of a tracked type are exported. Expected events use iCalendar `TENTATIVE`; confirmed events use `CONFIRMED`. Do not mark a date confirmed without evidence. Delayed entries retain their prior date in YAML but are excluded until a new date and exportable status are supplied.

Remove an event by deleting its mapping. Removing or filtering events removes them from the next full subscription feed; subscribers reconcile on refresh. This MVP publishes full snapshots, not cancellation notifications. Apple refresh behavior and timing vary, so verify edits in your subscribed calendar after refresh.

## Calendar behavior

Events have inclusive `DTSTART` and an exclusive next-day `DTEND`, both date-only. UIDs use `<event-id>@gaming-calendar`, independent of dates and titles; editing a date updates the same event identity. Keep this namespace unchanged once subscribed, and use globally distinctive IDs if running multiple copies of this project.

`DTSTAMP` and `LAST-MODIFIED` use the later of the event revision and available game-news revision. Explicit revisions keep generation deterministic and help clients recognize changes. Events are sorted by ID. Descriptions contain game, type, status, optional notes, optional game-level latest news and news source, and event source. There are no empty news sections. Events are transparent so they do not block your availability.

## Publish with GitHub Pages

To operate completely free, use a **public repository on GitHub Free**. Pages and standard hosted Actions for this public project require no paid services.

1. Push these files to your chosen GitHub repository's `main` branch. If your default branch has another name, change `main` in the workflow's push filter and both deployment conditions before pushing.
2. In **Settings → Actions → General**, allow GitHub Actions and the official `actions/*` actions if repository or organization policy currently blocks them. The workflow declares its own permissions; no repository-wide write permission or personal access token is needed.
3. In **Settings → Pages → Build and deployment → Source**, select **GitHub Actions**. Do not choose deployment from a branch.
4. In **Actions → Generate and publish calendar → Run workflow**, select `main` and run it. Later relevant pushes regenerate automatically. Pull requests validate and generate without deploying. Review any `github-pages` environment protection rules if deployment waits for approval.
5. Once both jobs succeed, use:
   `https://<username>.github.io/<repository>/gaming-calendar.ics`
   Replace the placeholders with your actual owner and repository. For an owner site repository named `<username>.github.io`, the URL is `https://<username>.github.io/gaming-calendar.ics`. A custom domain changes the host.
6. Open the direct `.ics` URL and verify the response starts with `BEGIN:VCALENDAR`. The root page may return 404 because this project intentionally has no website index.

The feed is public: never put secrets or private notes in the YAML. No scheduled workflow runs are needed because this MVP does not discover events. Manual dispatch is available for regeneration. GitHub outages and Apple refresh intervals can delay updates; this is a personal feed without service guarantees.

## Subscribe from Apple Calendar

On macOS, open **Calendar → File → New Calendar Subscription**, paste the HTTPS `.ics` URL, and click **Subscribe**. Choose a name and an auto-refresh interval (for example, daily). Choose iCloud as the location if offered and you want the subscription across devices. Use **View → Refresh Calendars** to request a refresh.

On iPhone/iPad, open **Settings → Apps → Calendar → Calendar Accounts → Add Account → Other → Add Subscribed Calendar** (older versions start at **Settings → Calendar → Accounts**), enter the URL, and save. An empty feed initially shows no game events; that is expected. Subscribe using the URL rather than downloading and importing the file, which would only create a one-time copy.

## Next step

First verify publishing and Apple subscription, then add a genuinely verified event with its source. Later, a separate process can discover reliable announcements and propose YAML changes with sources and revision timestamps. It can preserve IDs for date changes and update game-level news; this existing pipeline then handles validation and publishing. Research automation is intentionally outside this MVP.
