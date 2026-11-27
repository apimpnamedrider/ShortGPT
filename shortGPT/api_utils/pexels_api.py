import requests

from shortGPT.config.api_db import ApiKeyManager


def search_videos(query_string, orientation_landscape=True):
    api_key = ApiKeyManager.get_api_key("PEXELS_API_KEY")
    if not api_key:
        return {"videos": []}

    response = requests.get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": api_key},
        params={
            "query": query_string,
            "orientation": "landscape" if orientation_landscape else "portrait",
            "per_page": 15,
        },
        timeout=(8, 25),
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("videos"), list):
        raise RuntimeError("Pexels returned an unexpected response.")
    return payload


def getBestVideo(query_string, orientation_landscape=True, used_vids=None):
    used_vids = used_vids or []
    videos = search_videos(query_string, orientation_landscape).get("videos", [])
    target_ratio = 16 / 9 if orientation_landscape else 9 / 16
    candidates = []

    for video in videos:
        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        if not width or not height:
            continue
        ratio = width / height
        if abs(ratio - target_ratio) > 0.02:
            continue
        for video_file in video.get("video_files", []):
            link = video_file.get("link")
            if not link or not link.startswith("https://"):
                continue
            file_width = int(video_file.get("width") or 0)
            file_height = int(video_file.get("height") or 0)
            if orientation_landscape and file_width >= 1920 and file_height >= 1080:
                candidates.append((abs(15 - int(video.get("duration") or 15)), link))
            elif not orientation_landscape and file_width >= 1080 and file_height >= 1920:
                candidates.append((abs(15 - int(video.get("duration") or 15)), link))

    for _score, link in sorted(candidates, key=lambda item: item[0]):
        if link.split(".hd")[0] not in used_vids:
            return link
    return None
