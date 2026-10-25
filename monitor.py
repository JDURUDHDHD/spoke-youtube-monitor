
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

HEADERS = {"User-Agent": "YouTubeMonitor/4.0"}
MAX_SENT_HISTORY = 2000


def now_utc():
    return datetime.now(timezone.utc)


def today_utc():
    return now_utc().date().isoformat()


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
            f"YouTube API error {error.code}: {details[:500]}"
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
        "schema_version": 4,
        "entries": entries or {},
        "playlists": playlists or {},
        "pending_notifications": {},
        "sent_notifications": [],
        "digest_events": [],
        "digest_cursor": 0,
        "last_digest_date": today_utc(),
    }


def load_state():
    if not STATE_FILE.exists():
        return new_state(), True

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "state.json could not be read. Stopping to protect "
            "the existing notification history."
        ) from error

    version = state.get("schema_version")

    if version == 4:
        state.setdefault("entries", {})
        state.setdefault("playlists", {})
        state.setdefault("pending_notifications", {})
        state.setdefault("sent_notifications", [])
        state.setdefault("digest_events", [])
        state.setdefault("digest_cursor", 0)
        state.setdefault("last_digest_date", today_utc())
        return state, False

    if version == 3:
        # Preserve the existing baseline when upgrading.
        migrated = new_state(
            entries=state.get("entries", {}),
            playlists=state.get("playlists", {}),
        )
        print("Migrated state from version 3 to version 4.")
        return migrated, False

    if version == 2:
        # Preserve old video IDs. Baseline playlists on this run.
        migrated = new_state(entries=state.get("entries", {}))
        migrated["playlists"] = None
        print("Migrated state from version 2 to version 4.")
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
        print(f"Discord HTTP error {error.code}: {details[:500]}")
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


def dispatch_pending(state):
    # Persist the queue before attempting delivery. If a delivery fails,
    # the pending item remains saved for a later run.
    save_state(state)

    for event_id in list(state["pending_notifications"].keys()):
        item = state["pending_notifications"].get(event_id)
        if not item:
            continue

        # Do not mark an event sent until Discord accepts it.
        send_discord(item["payload"])

        state["pending_notifications"].pop(event_id, None)
        state["sent_notifications"].append(event_id)
        state["sent_notifications"] = state["sent_notifications"][
            -MAX_SENT_HISTORY:
        ]

        if item["type"] == "digest":
            state["last_digest_date"] = item["digest_date"]
            state["digest_cursor"] = item["digest_cursor_after"]

        # Save after every successful delivery so subsequent failures
        # do not normally resend earlier alerts.
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
    data = api_get("/playlistItems", {
        "part": "snippet,contentDetails,status",
        "playlistId": playlist_id,
        "maxResults": 50,
    })

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

    thumbnails = snippet.get("thumbnails", {})
    thumbnail = (
        thumbnails.get("high", {}).get("url")
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
        f"**{entry['title']}**\n"
        f"[Watch on YouTube]({video_url})\n"
        f"[Visit channel]({entry['channel_url']})"
    )

    event_id = f"video:{video_id}"
    payload = make_embed(
        label, description, url=video_url, image=thumbnail,
        color=color, fields=fields,
    )
    queue_notification(state, event_id, payload)
    record_digest_event(state, event_id, label, entry["title"], video_url)


def add_playlist_entry_alert(state, entry):
    video_url = f"https://www.youtube.com/watch?v={entry['video_id']}"
    privacy = entry["privacy"]

    if privacy == "unlisted":
        label = "🔎 Unlisted Video Found in Playlist"
        color = 9807270
    else:
        label = "➕ New Playlist Entry"
        color = 3447003

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
    payload = make_embed(
        label, description, url=video_url,
        image=entry.get("thumbnail"), color=color,
    )
    queue_notification(state, event_id, payload)
    record_digest_event(state, event_id, label, entry["title"], video_url)


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
    payload = make_embed(
        label, description, url=playlist_url, color=10181046,
    )
    queue_notification(state, event_id, payload)
    record_digest_event(state, event_id, label, playlist["title"], playlist_url)


def schedule_daily_digest(state):
    today = today_utc()
    digest_id = f"digest:{today}"

    if state["last_digest_date"] == today:
        return

    # If a previous digest is still queued, retry it first. The next run
    # can create today's digest after the pending one has been delivered.
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

    payload = make_embed(
        "📊 Daily YouTube Creator Digest",
        f"Activity summary for the four monitored channels.\n\n"
        f"{description}\n\n"
        f"*Digest date: {today} (UTC)*",
        color=5793266,
    )

    queue_notification(
        state,
        digest_id,
        payload,
        event_type="digest",
        digest_cursor_after=len(state["digest_events"]),
        digest_date=today,
    )


def main():
    state, first_run = load_state()
    old_entries = state.get("entries", {})
    old_playlists = state.get("playlists")

    current_entries = {}
    current_playlists = {}

    for handle in CHANNEL_HANDLES:
        print(f"Checking {handle}...")
        channel = get_channel(handle)
        playlists = get_playlists(channel)

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

        print(
            f"{channel['name']}: found {len(playlists)} "
            "public playlists including uploads"
        )

    if first_run:
        state["entries"] = current_entries
        state["playlists"] = current_playlists

        queue_notification(
            state,
            "startup:v4",
            make_embed(
                "✅ YouTube Monitor Started",
                "Monitoring all four configured channels. "
                "The existing public videos and playlists have been "
                "saved as the initial baseline.",
                color=3066993,
            ),
        )
        save_state(state)
        dispatch_pending(state)
        print("Initial baseline saved.")
        return

    # Version 2 migrations had no reliable playlist baseline. Establish
    # it now so existing playlists do not all trigger alerts.
    if old_playlists is None:
        old_playlists = current_playlists.copy()

    new_entry_keys = current_entries.keys() - old_entries.keys()
    new_playlist_keys = current_playlists.keys() - old_playlists.keys()

    new_upload_ids = {
        current_entries[key]["video_id"]
        for key in new_entry_keys
        if current_entries[key]["is_uploads"]
    }

    details = get_video_details(list(new_upload_ids))

    for video_id in sorted(new_upload_ids):
        entry = next(
            item for item in current_entries.values()
            if item["video_id"] == video_id and item["is_uploads"]
        )
        add_video_alert(state, entry, details.get(video_id, {}))

    for key in sorted(new_entry_keys):
        entry = current_entries[key]

        if entry["is_uploads"]:
            continue
        if entry["video_id"] in new_upload_ids:
            continue

        add_playlist_entry_alert(state, entry)

    for key in sorted(new_playlist_keys):
        playlist = current_playlists[key]
        if not playlist["is_uploads"]:
            add_new_playlist_alert(state, playlist)

    state["entries"] = current_entries
    state["playlists"] = current_playlists

    # Schedule at most one daily digest, using UTC dates.
    schedule_daily_digest(state)

    # Save discoveries and the queue before trying to send anything.
    save_state(state)
    dispatch_pending(state)

    print(f"New playlist entries: {len(new_entry_keys)}")
    print(f"New playlists: {len(new_playlist_keys)}")
    print(f"Notifications still pending: {len(state['pending_notifications'])}")
    print("Monitor finished successfully.")


if __name__ == "__main__":
    main()