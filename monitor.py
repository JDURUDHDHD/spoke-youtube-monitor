
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_KEY = os.environ["YOUTUBE_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

CHANNEL_ID = "UCk2uxbWi5py_iJXaEsh2YRA"
API = "https://www.googleapis.com/youtube/v3"
STATE_FILE = Path("state.json")

HEADERS = {
    "User-Agent": (
        "SpokeYouTubeMonitor/1.0 "
        "(https://github.com/JDURUDHDHD/spoke-youtube-monitor)"
    )
}


def api_get(endpoint, params):
    request_params = dict(params)
    request_params["key"] = API_KEY

    url = API + endpoint + "?" + urllib.parse.urlencode(request_params)
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
            print(f"Discord message sent. HTTP {response.status}")

    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        print(f"Discord HTTP error: {error.code}")
        print(f"Discord response: {details[:500]}")
        raise


def get_uploads_playlist():
    data = api_get(
        "/channels",
        {
            "part": "contentDetails",
            "id": CHANNEL_ID,
        },
    )

    items = data.get("items", [])
    if not items:
        raise RuntimeError(
            "YouTube channel not found. Check CHANNEL_ID."
        )

    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]


def collect_playlist(playlist_id):
    videos = {}
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
                continue

            videos[video_id] = {
                "title": snippet.get("title", "Untitled"),
                "publishedAt": details.get(
                    "videoPublishedAt",
                    snippet.get("publishedAt", ""),
                ),
                "playlist": playlist_id,
            }

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return videos


def collect():
    uploads_playlist = get_uploads_playlist()
    playlist_ids = {uploads_playlist}
    page_token = None

    while True:
        data = api_get(
            "/playlists",
            {
                "part": "id",
                "channelId": CHANNEL_ID,
                "maxResults": 50,
                **(
                    {"pageToken": page_token}
                    if page_token
                    else {}
                ),
            },
        )

        for item in data.get("items", []):
            playlist_ids.add(item["id"])

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    videos = {}

    for playlist_id in sorted(playlist_ids):
        try:
            videos.update(collect_playlist(playlist_id))
        except urllib.error.HTTPError as error:
            print(
                f"Could not read playlist {playlist_id}: "
                f"HTTP {error.code}"
            )

    return videos


def main():
    print("Checking YouTube channel...")
    current = collect()
    print(f"Found {len(current)} public playlist/upload entries.")

    if STATE_FILE.exists():
        try:
            previous = json.loads(
                STATE_FILE.read_text(encoding="utf-8")
            )
        except (json.JSONDecodeError, OSError):
            previous = {}

        new_ids = current.keys() - previous.keys()

        for video_id in sorted(new_ids):
            video = current[video_id]
            message = (
                "🆕 New YouTube entry detected\n"
                f"Title: {video['title']}\n"
                f"https://www.youtube.com/watch?v={video_id}"
            )
            discord(message)

        STATE_FILE.write_text(
            json.dumps(current, indent=2),
            encoding="utf-8",
        )

        print(f"Check complete. New entries: {len(new_ids)}")

    else:
        discord(
            "✅ Spoke monitor started. "
            f"Found {len(current)} public playlist/upload entries. "
            "This is the initial baseline; future checks will alert "
            "on newly detected entries."
        )

        STATE_FILE.write_text(
            json.dumps(current, indent=2),
            encoding="utf-8",
        )

        print("Initial baseline saved.")


if __name__ == "__main__":
    main()
