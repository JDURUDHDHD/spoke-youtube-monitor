
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

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
    "User-Agent": "SpokeYouTubeMonitor/2.0"
}


def api_get(endpoint, params):
    query = dict(params)
    query["key"] = API_KEY

    url = API + endpoint + "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(url, headers=HEADERS)

    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def discord(message):
    payload = json.dumps({"content": message}).encode("utf-8")

    request = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=payload,
        headers={
            **HEADERS,
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print(f"Discord message sent: HTTP {response.status}")
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        print(f"Discord HTTP error: {error.code}")
        print(f"Discord response: {details[:500]}")
        raise


def get_channel(handle):
    data = api_get(
        "/channels",
        {
            "part": "id,snippet,contentDetails",
            "forHandle": handle,
        },
    )

    items = data.get("items", [])
    if not items:
        raise RuntimeError(
            f"YouTube channel not found for handle {handle}"
        )

    channel = items[0]

    return {
        "id": channel["id"],
        "handle": handle,
        "name": channel["snippet"]["title"],
        "uploads": channel["contentDetails"]["relatedPlaylists"]["uploads"],
    }


def get_playlists(channel):
    playlists = {
        channel["uploads"]: "Uploads",
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
            playlists[item["id"]] = item["snippet"].get(
                "title", "Untitled playlist"
            )

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return playlists


def collect_playlist(channel, playlist_id, playlist_title):
    entries = {}
    page_token = None

    while True:
        params = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": 50,
        }

        if page_token:
            params["pageToken"] = page_token

        data = api_get("/playlistItems", params)

        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            details = item.get("contentDetails", {})

            video_id = details.get("videoId")
            if not video_id:
                resource = snippet.get("resourceId", {})
                video_id = resource.get("videoId")

            # Keep visible unavailable/deleted entries when an item ID exists.
            item_id = item.get("id")
            identity = video_id or item_id

            if not identity:
                continue

            key = f"{channel['id']}|{playlist_id}|{identity}"

            entries[key] = {
                "channel": channel["name"],
                "handle": channel["handle"],
                "playlist": playlist_title,
                "title": snippet.get("title", "Unavailable or untitled"),
                "video_id": video_id,
            }

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return entries


def collect_all():
    entries = {}

    for handle in CHANNEL_HANDLES:
        print(f"Checking {handle}...")
        channel = get_channel(handle)
        playlists = get_playlists(channel)

        print(
            f"Resolved {channel['name']}: "
            f"{len(playlists)} public playlists including uploads"
        )

        for playlist_id, playlist_title in playlists.items():
            found = collect_playlist(
                channel,
                playlist_id,
                playlist_title,
            )
            entries.update(found)

    return entries


def load_previous():
    if not STATE_FILE.exists():
        return None

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("Existing state could not be read; creating a new baseline.")
        return None

    # Older monitor versions used a different state format.
    if state.get("schema_version") != 2:
        print("Older state format detected; creating a new four-channel baseline.")
        return None

    return state.get("entries", {})


def save_state(entries):
    state = {
        "schema_version": 2,
        "entries": entries,
    }

    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def main():
    current = collect_all()
    previous = load_previous()

    print(f"Total public playlist entries found: {len(current)}")

    if previous is None:
        discord(
            "✅ **Four-channel YouTube monitor started!**\n"
            "Monitoring:\n"
            "• https://www.youtube.com/@Spokeishere\n"
            "• https://www.youtube.com/@wemmbumc\n"
            "• https://www.youtube.com/@flamefragsmc\n"
            "• https://www.youtube.com/@Parrotx2\n\n"
            f"Saved the initial baseline of {len(current)} entries. "
            "Newly detected entries will trigger alerts."
        )
        save_state(current)
        print("Initial baseline saved.")
        return

    new_keys = current.keys() - previous.keys()
    print(f"Newly detected playlist entries: {len(new_keys)}")

    # Send notifications before saving state. If a send fails,
    # the next run can retry instead of silently losing the alert.
    for key in sorted(new_keys):
        entry = current[key]
        video_id = entry.get("video_id")

        if video_id:
            link = f"https://www.youtube.com/watch?v={video_id}"
        else:
            link = (
                "https://www.youtube.com/"
                + entry["handle"]
            )

        discord(
            "🆕 **New YouTube playlist entry detected!**\n"
            f"Channel: **{entry['channel']}** "
            f"({entry['handle']})\n"
            f"Playlist: **{entry['playlist']}**\n"
            f"Title: {entry['title']}\n"
            f"{link}"
        )

    save_state(current)
    print("Monitor finished successfully.")


if __name__ == "__main__":
    main()