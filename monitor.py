
import time
import json
import os
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

API_KEY = os.environ["YOUTUBE_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
API = "https://www.googleapis.com/youtube/v3"
STATE_FILE = Path("state.json")

CHANNEL_HANDLES = [
    "@Spokeishere",
    "@wemmbumc",
    "@flamefragsmc",
    "@Parrotx2",
]

HEADERS = {"User-Agent": "YouTubeMonitor/5.1"}
MAX_SENT_HISTORY = 2000
MAX_DIGEST_EVENTS = 5000
RECENT_HOURS = 24


def now_utc():
    return datetime.now(timezone.utc)


def today_utc():
    return now_utc().date().isoformat()


def published_recently(detail):
    """True only when YouTube provides a recent publication date."""
    published_at = detail.get("snippet", {}).get("publishedAt")
    if not published_at:
        return False

    try:
        published = datetime.fromisoformat(
            published_at.replace("Z", "+00:00")
        )
        age = (now_utc() - published).total_seconds()
        return 0 <= age <= RECENT_HOURS * 60 * 60
    except (ValueError, TypeError):
        return False


def api_get(endpoint, params):
    params = dict(params)
    params["key"] = API_KEY
    url = API + endpoint + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers=HEADERS)

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"YouTube API error {error.code}: {details[:400]}"
        ) from error
    except (urllib.error.URLError, TimeoutError) as error:
        raise RuntimeError(
            f"YouTube API connection failed: {error}"
        ) from error


def save_state(state):
    temp_file = STATE_FILE.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temp_file.replace(STATE_FILE)


def new_state(entries=None, playlists=None):
    return {
        "schema_version": 5,
        "entries": entries or {},
        "playlists": playlists or {},
        "pending_notifications": {},
        "sent_notifications": [],
        "digest_events": [],
        "digest_cursor": 0,
        "last_digest_date": today_utc(),
        "unlisted_alerted": {},
        "last_scan_report": {},
    }


def load_state():
    if not STATE_FILE.exists():
        return new_state(), True

    try:
        state = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "state.json could not be read. Stopping to protect "
            "the existing notification history."
        ) from error

    version = state.get("schema_version")

    if version == 5:
        state.setdefault("entries", {})
        state.setdefault("playlists", {})
        state.setdefault("pending_notifications", {})
        state.setdefault("sent_notifications", [])
        state.setdefault("digest_events", [])
        state.setdefault("digest_cursor", 0)
        state.setdefault("last_digest_date", today_utc())
        state.setdefault("unlisted_alerted", {})
        state.setdefault("last_scan_report", {})
        return state, False

    if version == 4:
        state["schema_version"] = 5
        state.setdefault("entries", {})
        state.setdefault("playlists", {})
        state.setdefault("pending_notifications", {})
        state.setdefault("sent_notifications", [])
        state.setdefault("digest_events", [])
        state.setdefault("digest_cursor", 0)
        state.setdefault("last_digest_date", today_utc())
        state.setdefault("unlisted_alerted", {})
        state.setdefault("last_scan_report", {})
        print("Upgraded state from version 4 to version 5.")
        return state, False

    if version == 3:
        migrated = new_state(
            entries=state.get("entries", {}),
            playlists=state.get("playlists", {}),
        )
        print("Migrated state from version 3 to version 5.")
        return migrated, False

    if version == 2:
        migrated = new_state(entries=state.get("entries", {}))
        migrated["playlists"] = None
        print("Migrated state from version 2 to version 5.")
        return migrated, False

    raise RuntimeError(
        f"Unsupported state schema version: {version}. "
        "Stopping rather than overwriting saved history."
    )


def send_discord(payload):
    request = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=json.dumps({
            "embeds": [payload],
            "allowed_mentions": {"parse": []},
        }).encode("utf-8"),
        headers={
            **HEADERS,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print(f"Discord alert sent: HTTP {response.status}")
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        print(f"Discord HTTP error {error.code}: {details[:400]}")
        raise


def make_embed(title, description, url=None, image=None,
               color=3447003, fields=None):
    embed = {
        "title": title[:256],
        "description": description[:4000],
        "color": color,
        "timestamp": now_utc().isoformat(),
    }
    if url:
        embed["url"] = url
    if image:
        embed["thumbnail"] = {"url": image}
    if fields:
        embed["fields"] = fields[:25]
    return embed


def queue_notification(state, event_id, payload, event_type="alert",
                       digest_cursor_after=None, digest_date=None):
    if event_id in state["sent_notifications"]:
        return
    if event_id in state["pending_notifications"]:
        return

    state["pending_notifications"][event_id] = {
        "payload": payload,
        "type": event_type,
        "digest_cursor_after": digest_cursor_after,
        "digest_date": digest_date,
    }


def record_digest_event(state, event_id, title, description, url):
    if any(item["id"] == event_id for item in state["digest_events"]):
        return

    state["digest_events"].append({
        "id": event_id,
        "title": title,
        "description": description,
        "url": url,
    })
    state["digest_events"] = state["digest_events"][-MAX_DIGEST_EVENTS:]


def dispatch_pending(state):
    save_state(state)

    for event_id in list(state["pending_notifications"].keys()):
        item = state["pending_notifications"].get(event_id)
        if not item:
            continue

        sent = False

        for attempt in range(5):
            try:
                send_discord(item["payload"])
                sent = True
                break

            except urllib.error.HTTPError as error:
                if error.code != 429:
                    print(
                        f"Discord HTTP {error.code} for {event_id}; "
                        "leaving notification queued."
                    )
                    break

                retry_after = 1.0
                try:
                    details = json.loads(
                        error.read().decode("utf-8", errors="replace")
                    )
                    retry_after = float(details.get("retry_after", 1.0))
                except (ValueError, TypeError, json.JSONDecodeError):
                    pass

                wait_seconds = min(max(retry_after, 1.0), 60.0)
                print(f"Discord rate limit: waiting {wait_seconds}s.")
                time.sleep(wait_seconds)

            except (urllib.error.URLError, TimeoutError) as error:
                print(f"Network error for {event_id}: {error}")
                break

        if not sent:
            save_state(state)
            break

        state["pending_notifications"].pop(event_id, None)
        state["sent_notifications"].append(event_id)
        state["sent_notifications"] = state["sent_notifications"][
            -MAX_SENT_HISTORY:
        ]

        if item.get("type") == "digest":
            state["last_digest_date"] = item["digest_date"]
            state["digest_cursor"] = item["digest_cursor_after"]

        save_state(state)


def get_channel(handle):
    data = api_get("/channels", {
        "part": "id,snippet,contentDetails",
        "forHandle": handle,
    })
    items = data.get("items", [])
    if not items:
        raise RuntimeError(f"Channel not found: {handle}")

    item = items[0]
    return {
        "id": item["id"],
        "handle": handle,
        "name": item["snippet"]["title"],
        "uploads": item["contentDetails"]["relatedPlaylists"]["uploads"],
        "url": "https://www.youtube.com/" + handle,
    }


def get_playlists(channel):
    playlists = {
        channel["uploads"]: {
            "title": "Uploads",
            "is_uploads": True,
        }
    }
    page_token = None

    while True:
        params = {
            "part": "id,snippet",
            "channelId": channel["id"],
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token

        data = api_get("/playlists", params)

        for item in data.get("items", []):
            playlists[item["id"]] = {
                "title": item.get("snippet", {}).get(
                    "title", "Untitled playlist"
                ),
                "is_uploads": False,
            }

        page_token = data.get("nextPageToken")
        if not page_token:
            return playlists


def collect_playlist(channel, playlist_id, playlist_info):
    entries = {}
    page_token = None

    # Scan all pages of each accessible playlist.
    while True:
        params = {
            "part": "snippet,contentDetails,status",
            "playlistId": playlist_id,
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token

        data = api_get("/playlistItems", params)

        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            details = item.get("contentDetails", {})
            status = item.get("status", {})

            video_id = details.get("videoId") or snippet.get(
                "resourceId", {}
            ).get("videoId")

            if not video_id:
                continue

            key = f"{channel['id']}|{playlist_id}|{video_id}"
            thumbnails = snippet.get("thumbnails", {})

            entries[key] = {
                "channel_id": channel["id"],
                "channel": channel["name"],
                "handle": channel["handle"],
                "channel_url": channel["url"],
                "playlist_id": playlist_id,
                "playlist": playlist_info["title"],
                "is_uploads": playlist_info["is_uploads"],
                "title": snippet.get("title", "Untitled video"),
                "video_id": video_id,
                "privacy": status.get("privacyStatus", "unknown"),
                "thumbnail": (
                    thumbnails.get("high", {}).get("url")
                    or thumbnails.get("medium", {}).get("url")
                    or thumbnails.get("default", {}).get("url")
                ),
            }

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return entries


def get_video_details(video_ids):
    results = {}
    ids = list(dict.fromkeys(video_ids))

    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        if not batch:
            continue

        data = api_get("/videos", {
            "part": "snippet,status,liveStreamingDetails",
            "id": ",".join(batch),
            "maxResults": 50,
        })

        for item in data.get("items", []):
            results[item["id"]] = item

    return results


def add_video_alert(state, entry, detail):
    video_id = entry["video_id"]
    video_url = f"https://www.youtube.com/watch?v={video_id}"

    snippet = detail.get("snippet", {})
    status = detail.get("status", {})
    privacy = status.get("privacyStatus", entry["privacy"])
    broadcast = snippet.get("liveBroadcastContent", "none")

    if broadcast == "upcoming":
        label = "🎬 PREMIERE / UPCOMING BROADCAST"
        color = 15844367
    elif privacy == "unlisted":
        label = "🔎 UNLISTED VIDEO DETECTED"
        color = 9807270
    else:
        label = "🆕 NEW VIDEO"
        color = 3066993

    thumbnail = (
        snippet.get("thumbnails", {}).get("high", {}).get("url")
        or entry.get("thumbnail")
    )

    fields = [
        {"name": "Creator", "value": entry["channel"], "inline": True},
        {"name": "Visibility", "value": privacy, "inline": True},
    ]

    scheduled = detail.get("liveStreamingDetails", {}).get(
        "scheduledStartTime"
    )
    if scheduled:
        fields.append({
            "name": "Scheduled time (UTC)",
            "value": scheduled,
            "inline": False,
        })

    description = (
        f"**{snippet.get('title') or entry['title']}**\n"
        f"[Watch on YouTube]({video_url})\n"
        f"[Visit channel]({entry['channel_url']})"
    )

    event_id = f"video:{video_id}"
    queue_notification(
        state,
        event_id,
        make_embed(
            label, description, url=video_url, image=thumbnail,
            color=color, fields=fields,
        ),
    )
    record_digest_event(
        state, event_id, label, entry["title"], video_url
    )


def add_unlisted_alert(state, entry, detail):
    video_id = entry["video_id"]
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    title = detail.get("snippet", {}).get("title") or entry["title"]

    queue_notification(
        state,
        f"unlisted:{video_id}",
        make_embed(
            "🔎 UNLISTED VIDEO DETECTED",
            f"**{title}**\n"
            f"Creator: **{entry['channel']}**\n"
            f"Found through: **{entry['playlist']}**\n"
            f"[Open video]({video_url})",
            url=video_url,
            image=entry.get("thumbnail"),
            color=9807270,
            fields=[
                {"name": "Visibility", "value": "unlisted", "inline": True},
                {
                    "name": "Detection scope",
                    "value": "Found through an accessible playlist",
                    "inline": False,
                },
            ],
        ),
    )

    record_digest_event(
        state,
        f"unlisted:{video_id}",
        "Unlisted video detected",
        f"{entry['channel']}: {title}",
        video_url,
    )
    state["unlisted_alerted"][video_id] = True


def add_playlist_entry_alert(state, entry):
    video_url = f"https://www.youtube.com/watch?v={entry['video_id']}"
    privacy = entry["privacy"]

    label = (
        "🔎 Unlisted Video Found in Playlist"
        if privacy == "unlisted"
        else "➕ New Playlist Entry"
    )
    color = 9807270 if privacy == "unlisted" else 3447003

    description = (
        f"**{entry['title']}**\n"
        f"Creator: {entry['channel']}\n"
        f"Playlist: **{entry['playlist']}**\n"
        f"Visibility reported by API: **{privacy}**\n"
        f"[Open video]({video_url})"
    )

    event_id = (
        f"entry:{entry['channel_id']}|"
        f"{entry['playlist_id']}|{entry['video_id']}"
    )
    queue_notification(
        state,
        event_id,
        make_embed(
            label, description, url=video_url,
            image=entry.get("thumbnail"), color=color,
        ),
    )
    record_digest_event(
        state, event_id, label, entry["title"], video_url
    )

    if privacy == "unlisted":
        state["unlisted_alerted"][entry["video_id"]] = True


def add_new_playlist_alert(state, playlist):
    playlist_url = (
        "https://www.youtube.com/playlist?list="
        + playlist["playlist_id"]
    )
    label = "📂 NEW PUBLIC PLAYLIST DISCOVERED"

    description = (
        f"**{playlist['title']}**\n"
        f"Creator: {playlist['channel']}\n"
        f"[Open playlist]({playlist_url})\n"
        f"[Visit channel]({playlist['channel_url']})"
    )

    event_id = f"playlist:{playlist['channel_id']}|{playlist['playlist_id']}"
    queue_notification(
        state,
        event_id,
        make_embed(
            label, description, url=playlist_url, color=10181046
        ),
    )
    record_digest_event(
        state, event_id, label, playlist["title"], playlist_url
    )


def queue_scan_error(state, error_text):
    event_id = f"scan-error:{today_utc()}"

    queue_notification(
        state,
        event_id,
        make_embed(
            "⚠️ YouTube Monitor Scan Failed",
            "The scan did not complete. The previous video and playlist "
            "baseline was preserved.\n\n"
            f"**Error:** {error_text[:2500]}\n\n"
            "The next scheduled run will try again.",
            color=15158332,
        ),
    )


def schedule_daily_digest(state):
    today = today_utc()

    if state["last_digest_date"] == today:
        return

    if any(
        item.get("type") == "digest"
        for item in state["pending_notifications"].values()
    ):
        return

    cursor = state["digest_cursor"]
    events = state["digest_events"][cursor:]
    lines = []

    for event in events[:20]:
        lines.append(
            f"• **{event['title']}** — {event['description']} "
            f"([Open]({event['url']}))"
        )

    if not lines:
        description = (
            "No new videos or playlists were detected since the "
            "previous daily digest."
        )
    else:
        description = "\n".join(lines)
        if len(events) > 20:
            description += (
                f"\n\n…and {len(events) - 20} more detected events."
            )

    report = state.get("last_scan_report", {})
    report_text = (
        "**Last successful scan report**\n"
        f"Channels checked: {report.get('channels', 0)}\n"
        f"Accessible playlist entries: {report.get('entries', 0)}\n"
        f"Video details returned: {report.get('videos_checked', 0)}\n"
        f"Unlisted videos identified: {report.get('unlisted_found', 0)}\n"
        f"Result: {report.get('status', 'No successful scan recorded')}"
    )

    queue_notification(
        state,
        f"digest:{today}",
        make_embed(
            "📊 Daily YouTube Creator Digest",
            f"{report_text}\n\n{description}\n\n"
            f"*Digest date: {today} (UTC)*",
            color=5793266,
        ),
        event_type="digest",
        digest_cursor_after=len(state["digest_events"]),
        digest_date=today,
    )


def main():
    state, first_run = load_state()

    if not state.get("pending_backlog_cleared_v1"):
        old_count = len(state.get("pending_notifications", {}))
        state["pending_notifications"] = {}
        state["digest_cursor"] = len(state.get("digest_events", []))
        state["pending_backlog_cleared_v1"] = True
        save_state(state)
        print(f"Cleared {old_count} old queued notifications.")
        
    old_entries = state.get("entries", {})
    old_playlists = state.get("playlists")

    current_entries = {}
    current_playlists = {}
    channels_checked = 0

    try:
        for handle in CHANNEL_HANDLES:
            print(f"Checking {handle}...")
            channel = get_channel(handle)
            playlists = get_playlists(channel)
            channels_checked += 1

            for playlist_id, info in playlists.items():
                playlist_key = f"{channel['id']}|{playlist_id}"

                current_playlists[playlist_key] = {
                    "channel_id": channel["id"],
                    "channel": channel["name"],
                    "handle": channel["handle"],
                    "channel_url": channel["url"],
                    "playlist_id": playlist_id,
                    "title": info["title"],
                    "is_uploads": info["is_uploads"],
                }

                current_entries.update(
                    collect_playlist(channel, playlist_id, info)
                )

        unique_ids = list(dict.fromkeys(
            entry["video_id"] for entry in current_entries.values()
        ))
        details = get_video_details(unique_ids)

    except Exception as error:
        print(f"SCAN FAILED: {error}")
        queue_scan_error(state, str(error))
        save_state(state)
        dispatch_pending(state)
        raise

    # Refresh the privacy information using video details when available.
    for entry in current_entries.values():
        detail = details.get(entry["video_id"], {})
        status = detail.get("status", {})
        if status.get("privacyStatus"):
            entry["privacy"] = status["privacyStatus"]

    if first_run:
        state["entries"] = current_entries
        state["playlists"] = current_playlists

        unlisted_ids = {
            entry["video_id"]
            for entry in current_entries.values()
            if entry["privacy"] == "unlisted"
        }
        for video_id in unlisted_ids:
            state["unlisted_alerted"][video_id] = True

        state["last_scan_report"] = {
            "status": "SUCCESS — initial baseline created",
            "channels": channels_checked,
            "entries": len(current_entries),
            "videos_checked": len(details),
            "unlisted_found": len(unlisted_ids),
            "checked_at": now_utc().isoformat(),
        }

        queue_notification(
            state,
            "startup:v5",
            make_embed(
                "✅ YouTube Monitor Started",
                "Monitoring all four configured channels. "
                "Existing accessible videos and playlists were saved "
                "as the initial baseline.\n\n"
                f"Accessible entries: {len(current_entries)}\n"
                f"Video details returned: {len(details)}\n"
                f"Unlisted videos identified: {len(unlisted_ids)}\n"
                "Unlisted detection is limited to discoverable video IDs.",
                color=3066993,
            ),
        )
        save_state(state)
        dispatch_pending(state)
        print("Initial baseline saved.")
        return

    if old_playlists is None:
        old_playlists = current_playlists.copy()

    # Find entries not in the saved baseline.
    discovered_keys = current_entries.keys() - old_entries.keys()

    # Only announce newly discovered videos whose actual publication date
    # is within the last 24 hours. Save older entries silently below.
    new_entry_keys = {
        key for key in discovered_keys
        if published_recently(
            details.get(current_entries[key]["video_id"], {})
        )
    }

    new_playlist_keys = current_playlists.keys() - old_playlists.keys()

    new_upload_ids = {
        current_entries[key]["video_id"]
        for key in new_entry_keys
        if current_entries[key]["is_uploads"]
    }

    for video_id in sorted(new_upload_ids):
        entry = next(
            item for item in current_entries.values()
            if item["video_id"] == video_id and item["is_uploads"]
        )
        add_video_alert(state, entry, details.get(video_id, {}))

    # Alert only once when a recently published, discovered video is
    # reported as unlisted. Older discoveries are marked as handled.
    for entry in current_entries.values():
        video_id = entry["video_id"]
        detail = details.get(video_id, {})
        privacy = detail.get("status", {}).get(
            "privacyStatus", entry["privacy"]
        )

        if privacy != "unlisted":
            continue
        if state["unlisted_alerted"].get(video_id):
            continue

        if published_recently(detail):
            add_unlisted_alert(state, entry, detail)
        else:
            state["unlisted_alerted"][video_id] = True


    unlisted_ids = {
        entry["video_id"]
        for entry in current_entries.values()
        if entry["privacy"] == "unlisted"
    }

    state["last_scan_report"] = {
        "status": "SUCCESS",
        "channels": channels_checked,
        "entries": len(current_entries),
        "videos_checked": len(details),
        "unlisted_found": len(unlisted_ids),
        "checked_at": now_utc().isoformat(),
    }

    # Save every discovered entry, including old ones intentionally
    # suppressed above, so they won't trigger alerts on future scans.
    state["entries"] = current_entries
    state["playlists"] = current_playlists

    schedule_daily_digest(state)
    save_state(state)
    dispatch_pending(state)

    print(f"Channels checked: {channels_checked}")
    print(f"Accessible entries: {len(current_entries)}")
    print(f"Video details returned: {len(details)}")
    print(f"Unlisted videos identified: {len(unlisted_ids)}")
    print(f"Newly discovered recent entries: {len(new_entry_keys)}")
    print(f"New playlists: {len(new_playlist_keys)}")
    print(f"Notifications pending: {len(state['pending_notifications'])}")
    print("Monitor finished successfully.")


if __name__ == "__main__":
    main()
