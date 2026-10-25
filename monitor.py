import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

API_KEY = os.environ["YOUTUBE_API_KEY"]
WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
CHANNEL_ID = "UCk2uxbWi5py_iJXaEsh2YRA"
STATE_FILE = Path("state.json")
API = "https://www.googleapis.com/youtube/v3/"


def api_get(endpoint, params):
    params["key"] = API_KEY
    url = API + endpoint + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(request, timeout=30):

        pass

except urllib.error.HTTPError as e:

    details = e.read().decode("utf-8", errors="replace")

    print(f"Discord HTTP error: {e.code}")

    print(f"Discord response: {details[:500]}")


def discord(message):
    data = json.dumps({
        "content": message[:1900],
        "allowed_mentions": {"parse": []}
    }).encode()
    request = urllib.request.Request(
        WEBHOOK, data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(request, timeout=30):
        pass


def pages(endpoint, params):
    token = None
    while True:
        query = dict(params, maxResults=50)
        if token:
            query["pageToken"] = token
        result = api_get(endpoint, query)
        yield from result.get("items", [])
        token = result.get("nextPageToken")
        if not token:
            break


def get_channel():
    result = api_get("channels", {
        "part": "contentDetails",
        "id": CHANNEL_ID
    })
    items = result.get("items", [])
    if not items:
        raise RuntimeError("Channel not found by the YouTube API.")
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def collect():
    playlists = {}
    uploads = []
    unavailable = {}

    for item in pages("playlists", {
        "part": "snippet",
        "channelId": CHANNEL_ID
    }):
        pid = item["id"]
        playlists[pid] = item["snippet"].get("title", "Untitled playlist")

    uploads_playlist = get_channel()

    for item in pages("playlistItems", {
        "part": "snippet,contentDetails",
        "playlistId": uploads_playlist
    }):
        video_id = item.get("contentDetails", {}).get("videoId")
        if video_id:
            uploads.append(video_id)

    for pid, playlist_title in playlists.items():
        for item in pages("playlistItems", {
            "part": "snippet,contentDetails",
            "playlistId": pid
        }):
            snippet = item.get("snippet", {})
            content = item.get("contentDetails", {})
            title = snippet.get("title", "Unknown video")
            video_id = content.get("videoId")
            low_title = title.lower()

            is_unavailable = (
                "private video" in low_title
                or "deleted video" in low_title
                or "unavailable video" in low_title
                or not video_id
            )

            if is_unavailable:
                position = snippet.get("position", -1)
                key = f"{pid}:{position}:{title}"
                unavailable[key] = {
                    "playlist": playlist_title,
                    "title": title,
                    "video_id": video_id
                }

    return {
        "playlists": playlists,
        "uploads": sorted(set(uploads)),
        "unavailable": unavailable
    }


def main():
    current = collect()
    previous = None

    if STATE_FILE.exists():
        previous = json.loads(STATE_FILE.read_text())

    if previous is None:
        STATE_FILE.write_text(json.dumps(current, indent=2))
        discord(
            "✅ Spoke monitor started. Baseline saved for public playlists "
            "and uploads. I'll alert on newly detected changes."
        )
        return

    alerts = []

    for pid, title in current["playlists"].items():
        if pid not in previous["playlists"]:
            alerts.append(
                f"📂 New public playlist: **{title}**\n"
                f"https://www.youtube.com/playlist?list={pid}"
            )

    old_uploads = set(previous["uploads"])
    for video_id in current["uploads"]:
        if video_id not in old_uploads:
            alerts.append(
                f"🎬 New public upload!\n"
                f"https://www.youtube.com/watch?v={video_id}"
            )

    old_unavailable = set(previous["unavailable"])
    for key, item in current["unavailable"].items():
        if key not in old_unavailable:
            alerts.append(
                f"⚠️ Potential unavailable playlist entry\n"
                f"Playlist: {item['playlist']}\n"
                f"Entry: {item['title']}"
            )

    # Save only after the API checks and notifications succeed.
    for start in range(0, len(alerts), 5):
        discord("\n\n".join(alerts[start:start + 5]))

    STATE_FILE.write_text(json.dumps(current, indent=2))

    if alerts:
        print(f"Sent {len(alerts)} alert(s).")
    else:
        print("No new public changes detected.")


if __name__ == "__main__":
    main()
