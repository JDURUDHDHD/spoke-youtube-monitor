
import json
import os
import urllib.error
import urllib.parse
import urllib.request
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

HEADERS = {
    "User-Agent": "YouTubeMonitor/3.0",
}

def api_get(endpoint, params):
    query = dict(params)
    query["key"] = API_KEY
    url = API + endpoint + "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(url, headers=HEADERS)

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"YouTube API error {error.code}: {details[:500]}"
        ) from error

def discord_embed(title, description, url=None, image=None,
                  color=3447003, fields=None):
    embed = {
        "title": title[:256],
        "description": description[:4000],
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if url:
        embed["url"] = url
    if image:
        embed["thumbnail"] = {"url": image}
    if fields:
        embed["fields"] = fields[:25]

    payload = {
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }

    request = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=json.dumps(payload).encode("utf-8"),
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
            break

    return playlists

def collect_playlist(channel, playlist_id, playlist_info):
    entries = {}
    page_token = None

    # Check the newest 50 items in each playlist per run.
    # This keeps the monitor's API usage more manageable.
    params = {
        "part": "snippet,contentDetails,status",
        "playlistId": playlist_id,
        "maxResults": 50,
    }

    data = api_get("/playlistItems", params)

    for item in data.get("items", []):
        snippet = item.get("snippet", {})
        details = item.get("contentDetails", {})
        status = item.get("status", {})

        video_id = details.get("videoId")
        if not video_id:
            video_id = snippet.get("resourceId", {}).get("videoId")

        if not video_id:
            continue

        key = f"{channel['id']}|{playlist_id}|{video_id}"

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
                snippet.get("thumbnails", {}).get("high", {}).get("url")
                or snippet.get("thumbnails", {}).get("medium", {}).get("url")
            ),
            "published_at": details.get("videoPublishedAt")
                or snippet.get("publishedAt"),
        }

    return entries

def get_video_details(video_ids):
    details = {}

    # YouTube videos.list accepts up to 50 IDs per request.
    unique_ids = list(dict.fromkeys(video_ids))

    for start in range(0, len(unique_ids), 50):
        batch = unique_ids[start:start + 50]
        if not batch:
            continue

        data = api_get("/videos", {
            "part": "snippet,status,liveStreamingDetails",
            "id": ",".join(batch),
            "maxResults": 50,
        })

        for video in data.get("items", []):
            details[video["id"]] = video

    return details

def load_previous():
    if not STATE_FILE.exists():
        return None

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("State file could not be read; creating a fresh baseline.")
        return None

    version = state.get("schema_version")

    if version == 3:
        return {
            "entries": state.get("entries", {}),
            "playlists": state.get("playlists", {}),
        }

    # Migrate the previous monitor's state without treating all existing
    # videos as new. Public playlists will be baselined on this first run.
    if version == 2:
        print("Migrating existing state to version 3.")
        return {
            "entries": state.get("entries", {}),
            "playlists": None,
        }

    print("No compatible state found; creating a baseline.")
    return None

def save_state(entries, playlists):
    state = {
        "schema_version": 3,
        "entries": entries,
        "playlists": playlists,
    }

    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

def main():
    current_entries = {}
    current_playlists = {}

    channels = []

    # Resolve all four channels and list their public playlists.
    for handle in CHANNEL_HANDLES:
        print(f"Checking {handle}...")
        channel = get_channel(handle)
        channels.append(channel)

        playlists = get_playlists(channel)

        for playlist_id, info in playlists.items():
            playlist_key = f"{channel['id']}|{playlist_id}"

            current_playlists[playlist_key] = {
                "channel": channel["name"],
                "handle": channel["handle"],
                "channel_url": channel["url"],
                "playlist_id": playlist_id,
                "title": info["title"],
                "is_uploads": info["is_uploads"],
            }

            found = collect_playlist(channel, playlist_id, info)
            current_entries.update(found)

        print(
            f"{channel['name']}: found {len(playlists)} "
            "public playlists, including uploads"
        )

    previous = load_previous()

    if previous is None:
        save_state(current_entries, current_playlists)
        discord_embed(
            "✅ YouTube Monitor Started",
            "Monitoring all four configured channels.\n\n"
            f"Saved a baseline of {len(current_entries)} playlist entries "
            f"and {len(current_playlists)} playlists. "
            "Newly detected items will trigger alerts.",
            color=3066993,
        )
        print("Initial baseline saved.")
        return

    previous_entries = previous["entries"]
    previous_playlists = previous["playlists"]

    new_entry_keys = current_entries.keys() - previous_entries.keys()

    if previous_playlists is None:
        # Migration: baseline playlists now, so existing playlists don't
        # all generate false "new playlist" alerts.
        new_playlist_keys = set()
    else:
        new_playlist_keys = (
            current_playlists.keys() - previous_playlists.keys()
        )

    # Inspect new video IDs for upcoming broadcasts/Premieres.
    new_video_ids = [
        current_entries[key]["video_id"]
        for key in new_entry_keys
    ]
    video_details = get_video_details(new_video_ids)

    # Prefer one upload alert per video, even if it is also in a playlist.
    new_upload_ids = {
        current_entries[key]["video_id"]
        for key in new_entry_keys
        if current_entries[key]["is_uploads"]
    }

    for video_id in sorted(new_upload_ids):
        matching = next(
            entry for entry in current_entries.values()
            if entry["video_id"] == video_id and entry["is_uploads"]
        )

        detail = video_details.get(video_id, {})
        snippet = detail.get("snippet", {})
        status = detail.get("status", {})

        privacy = status.get("privacyStatus", matching["privacy"])
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
            or matching.get("thumbnail")
        )

        fields = [
            {"name": "Creator", "value": matching["channel"], "inline": True},
            {"name": "Visibility", "value": privacy, "inline": True},
        ]

        scheduled = detail.get("liveStreamingDetails", {}).get(
            "scheduledStartTime"
        )
        if scheduled:
            fields.append({
                "name": "Scheduled time",
                "value": scheduled,
                "inline": False,
            })

        discord_embed(
            label,
            f"**{matching['title']}**\n"
            f"[Watch on YouTube](https://www.youtube.com/watch?v={video_id})\n"
            f"[Visit channel]({matching['channel_url']})",
            url=f"https://www.youtube.com/watch?v={video_id}",
            image=thumbnail,
            color=color,
            fields=fields,
        )

    # Detect new playlist entries outside the uploads playlist. These
    # can also reveal an unlisted video when YouTube exposes its status.
    for key in sorted(new_entry_keys):
        entry = current_entries[key]

        if entry["is_uploads"]:
            continue

        if entry["video_id"] in new_upload_ids:
            continue

        privacy = entry["privacy"]
        if privacy == "unlisted":
            title = "🔎 Unlisted Video Found in Playlist"
            color = 9807270
        else:
            title = "➕ New Playlist Entry"
            color = 3447003

        discord_embed(
            title,
            f"**{entry['title']}**\n"
            f"Creator: {entry['channel']}\n"
            f"Playlist: **{entry['playlist']}**\n"
            f"Visibility reported by API: **{privacy}**\n"
            f"[Open video](https://www.youtube.com/watch?v={entry['video_id']})",
            url=f"https://www.youtube.com/watch?v={entry['video_id']}",
            image=entry.get("thumbnail"),
            color=color,
        )

    # Detect newly created publicly listed playlists.
    for key in sorted(new_playlist_keys):
        playlist = current_playlists[key]

        if playlist["is_uploads"]:
            continue

        discord_embed(
            "📂 NEW PLAYLIST CREATED / DISCOVERED",
            f"**{playlist['title']}**\n"
            f"Creator: {playlist['channel']}\n"
            f"[Open playlist]("
            f"https://www.youtube.com/playlist?list={playlist['playlist_id']})\n"
            f"[Visit channel]({playlist['channel_url']})",
            url=(
                "https://www.youtube.com/playlist?list="
                + playlist["playlist_id"]
            ),
            color=10181046,
        )

    # Save only after notifications succeed, so failed alerts can retry.
    save_state(current_entries, current_playlists)

    print(f"New playlist entries: {len(new_entry_keys)}")
    print(f"New playlists: {len(new_playlist_keys)}")
    print("Monitor finished successfully.")

discord_embed("🧪 Test Alert", "Your YouTube monitor can send Discord messages.", color=3066993)

if __name__ == "__main__":
    main()
