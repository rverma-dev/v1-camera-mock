"""Jellyfin API client for auto-discovering video streams from collections."""

import json
import logging
import os
import urllib.request
import urllib.error
import urllib.parse

log = logging.getLogger("camera-mock.jellyfin")


def _resolve_env(value: str) -> str:
    """Resolve ${ENV_VAR} placeholders in a string."""
    if value and value.startswith("${") and value.endswith("}"):
        env_name = value[2:-1]
        resolved = os.environ.get(env_name, "")
        if not resolved:
            log.warning("env var %s is not set", env_name)
        return resolved
    return value


def _api_get(base_url: str, path: str, api_key: str, params: dict = None) -> dict:
    """Make an authenticated GET request to the Jellyfin API."""
    url = f"{base_url.rstrip('/')}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(url)
    req.add_header("X-Emby-Token", api_key)
    req.add_header("Accept", "application/json")

    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _find_collection_id(base_url: str, api_key: str, name: str) -> str | None:
    """Find a collection/playlist/library by display name."""
    # Search across views (libraries), boxsets (collections), and playlists
    for item_type in ["CollectionFolder", "BoxSet", "Playlist"]:
        try:
            data = _api_get(base_url, "/Items", api_key, {
                "searchTerm": name,
                "IncludeItemTypes": item_type,
                "Recursive": "true",
                "Limit": "5",
            })
            for item in data.get("Items", []):
                if item.get("Name", "").lower() == name.lower():
                    return item["Id"]
        except urllib.error.URLError as e:
            log.debug("search for %s type %s failed: %s", name, item_type, e)

    # Fallback: search in user views (top-level libraries)
    try:
        data = _api_get(base_url, "/Library/VirtualFolders", api_key)
        for folder in data:
            if folder.get("Name", "").lower() == name.lower():
                return folder.get("ItemId")
    except urllib.error.URLError as e:
        log.debug("virtual folders lookup failed: %s", e)

    return None


def _get_collection_items(
    base_url: str, api_key: str, collection_id: str, max_items: int
) -> list[dict]:
    """Get video items from a collection/playlist/library."""
    data = _api_get(base_url, f"/Items", api_key, {
        "ParentId": collection_id,
        "IncludeItemTypes": "Movie,Episode,Video",
        "Recursive": "true",
        "SortBy": "SortName",
        "SortOrder": "Ascending",
        "Limit": str(max_items),
        "Fields": "Path,MediaSources",
    })
    return data.get("Items", [])


def discover_streams(config: dict) -> list[dict]:
    """Discover streams from Jellyfin based on config.

    Returns a list of stream dicts:
        [{"name": "...", "source": "http://...", "mount": "/streamN"}, ...]
    """
    jf = config.get("jellyfin")
    if not jf:
        return []

    base_url = jf.get("url", "").rstrip("/")
    api_key = _resolve_env(jf.get("api_key", ""))
    max_streams = jf.get("max_streams", 8)
    loop_playback = jf.get("loop", True)

    if not base_url or not api_key:
        log.warning("jellyfin.url and jellyfin.api_key are required for auto-discover")
        return []

    # Resolve collection ID
    collection_id = jf.get("collection_id")
    if not collection_id:
        collection_name = jf.get("collection")
        if not collection_name:
            log.warning("jellyfin.collection or jellyfin.collection_id required")
            return []
        log.info("searching for Jellyfin collection: %s", collection_name)
        collection_id = _find_collection_id(base_url, api_key, collection_name)
        if not collection_id:
            log.error("collection '%s' not found in Jellyfin", collection_name)
            return []
        log.info("found collection '%s' → id=%s", collection_name, collection_id)

    items = _get_collection_items(base_url, api_key, collection_id, max_streams)
    if not items:
        log.warning("no video items found in collection %s", collection_id)
        return []

    streams = []
    for i, item in enumerate(items, start=1):
        item_id = item["Id"]
        item_name = item.get("Name", f"video-{i}")

        # Prefer direct stream URL (raw file bytes, avoids transcoding)
        source_url = (
            f"{base_url}/Videos/{item_id}/stream"
            f"?static=true&api_key={api_key}"
        )

        # Check if local file path is accessible (for native Pi deployment)
        local_path = item.get("Path")
        if local_path and os.path.isfile(local_path):
            log.info("stream%d: using local file %s", i, local_path)
            source = local_path
        else:
            log.info("stream%d: using HTTP stream for '%s'", i, item_name)
            source = source_url

        streams.append({
            "name": item_name,
            "source": source,
            "mount": f"/stream{i}",
            "loop": loop_playback,
        })

    log.info("discovered %d streams from Jellyfin", len(streams))
    return streams
