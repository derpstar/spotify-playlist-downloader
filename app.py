from __future__ import annotations

import argparse
import csv
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from re import sub
from urllib.parse import urlencode, urlparse, parse_qs

try:
    import requests
except ModuleNotFoundError:
    requests = None

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"
REQUEST_TIMEOUT = 30
CALLBACK_PATH = "/callback"
SPOTIFY_SCOPES = "playlist-read-private playlist-read-collaborative"
VERBOSE = False

DATA_DIR = Path(os.environ.get("SPOTIFY_DATA_DIR", "/data"))
APP_DIR = Path(__file__).resolve().parent
VERSION_FILE = APP_DIR / "VERSION"
TOKEN_CACHE = DATA_DIR / "token.json"
STATE_CACHE = DATA_DIR / "oauth_state.txt"
DEFAULT_REDIRECT_URI = os.environ.get("SPOTIFY_REDIRECT_URI", f"http://127.0.0.1:8888{CALLBACK_PATH}")
DEFAULT_CALLBACK_HOST = os.environ.get("SPOTIFY_CALLBACK_HOST", "0.0.0.0")
DEFAULT_PORT = int(urlparse(DEFAULT_REDIRECT_URI).port or 8888)
MINIMAL_FIELDS = ["track_name", "artists"]
OWNED_INDEX_FIELDS = ["name", "id", "tracks", "output"]
PLAYLIST_METADATA_FIELDS = ["name", "id", "owner", "owner_id", "tracks_total", "public", "spotify_url", "share_url"]
CHOSIC_QUEUE_FIELDS = ["status", "name", "id", "tracks_total", "owner", "share_url", "export_file", "notes"]
DEFAULT_SHARE_TOKEN = os.environ.get("SPOTIFY_SHARE_TOKEN", "local-export")


class SpotifyExporterError(Exception):
    pass


def set_verbose(enabled: bool) -> None:
    global VERBOSE
    VERBOSE = enabled


def debug(message: str) -> None:
    if VERBOSE:
        print(f"[debug] {message}", file=sys.stderr)


def require_requests() -> None:
    if requests is None:
        raise SpotifyExporterError(
            "The 'requests' package is required for Spotify API commands. Install dependencies or use Docker."
        )


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def load_env(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise SpotifyExporterError(f"Missing required environment variable: {name}")
    return value


def write_private_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def save_json(path: Path, payload: dict) -> None:
    write_private_text(path, json.dumps(payload, indent=2))


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_auth_url(client_id: str, redirect_uri: str, scope: str) -> str:
    ensure_data_dir()
    state = secrets.token_urlsafe(24)
    write_private_text(STATE_CACHE, state)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "show_dialog": "false",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code_for_token(client_id: str, client_secret: str, redirect_uri: str, code: str) -> dict:
    require_requests()
    debug(f"Exchanging authorization code for token via {TOKEN_URL}")
    try:
        response = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SpotifyExporterError(f"Token exchange request failed: {exc}") from exc
    if not response.ok:
        raise SpotifyExporterError(f"Token exchange failed: {response.status_code} {response.text}")
    debug(f"Token exchange succeeded with status {response.status_code}")
    token_payload = response.json()
    token_payload["obtained_at"] = int(time.time())
    save_json(TOKEN_CACHE, token_payload)
    return token_payload


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    require_requests()
    debug(f"Refreshing Spotify access token via {TOKEN_URL}")
    try:
        response = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SpotifyExporterError(f"Token refresh request failed: {exc}") from exc
    if not response.ok:
        raise SpotifyExporterError(f"Token refresh failed: {response.status_code} {response.text}")
    debug(f"Token refresh succeeded with status {response.status_code}")
    refreshed = response.json()
    refreshed["refresh_token"] = refreshed.get("refresh_token", refresh_token)
    refreshed["obtained_at"] = int(time.time())
    save_json(TOKEN_CACHE, refreshed)
    return refreshed


def get_valid_token(client_id: str, client_secret: str) -> str:
    if not TOKEN_CACHE.exists():
        raise SpotifyExporterError("No cached token found. Run the auth command first.")
    token_payload = load_json(TOKEN_CACHE)
    expires_in = int(token_payload.get("expires_in", 0))
    obtained_at = int(token_payload.get("obtained_at", 0))
    if int(time.time()) >= obtained_at + expires_in - 60:
        debug("Cached token is near expiry; refreshing")
        refresh_token = token_payload.get("refresh_token")
        if not refresh_token:
            raise SpotifyExporterError("Cached token expired and no refresh token is available.")
        token_payload = refresh_access_token(client_id, client_secret, refresh_token)
    else:
        debug("Using cached Spotify access token")
    return token_payload["access_token"]


def api_get(path: str, token: str, params: dict | None = None) -> dict:
    require_requests()
    debug(f"GET {API_BASE}{path} params={params or {}}")
    try:
        response = requests.get(
            f"{API_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise SpotifyExporterError(f"Spotify API request failed: {exc}") from exc
    debug(f"GET {API_BASE}{path} -> {response.status_code}")
    if not response.ok:
        raise SpotifyExporterError(f"Spotify API request failed: {response.status_code} {response.text}")
    return response.json()


def api_get_response(path: str, token: str, params: dict | None = None) -> requests.Response:
    require_requests()
    debug(f"GET {API_BASE}{path} params={params or {}}")
    try:
        response = requests.get(
            f"{API_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        debug(f"GET {API_BASE}{path} -> {response.status_code}")
        return response
    except requests.RequestException as exc:
        raise SpotifyExporterError(f"Spotify API request failed: {exc}") from exc


def paginate(path: str, token: str, params: dict | None = None):
    require_requests()
    next_url = f"{API_BASE}{path}"
    next_params = params.copy() if params else {}
    seen_urls: set[str] = set()
    api_netloc = urlparse(API_BASE).netloc
    while next_url:
        if next_url in seen_urls:
            raise SpotifyExporterError(f"Spotify pagination loop detected at {next_url}")
        seen_urls.add(next_url)
        debug(f"GET {next_url} params={next_params or {}}")
        try:
            response = requests.get(
                next_url,
                headers={"Authorization": f"Bearer {token}"},
                params=next_params,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise SpotifyExporterError(f"Spotify API request failed: {exc}") from exc
        debug(f"GET {next_url} -> {response.status_code}")
        if not response.ok:
            raise SpotifyExporterError(f"Spotify API request failed: {response.status_code} {response.text}")
        data = response.json()
        if not isinstance(data, dict):
            raise SpotifyExporterError("Spotify pagination response was not a JSON object.")
        items = data.get("items") or []
        if not isinstance(items, list):
            raise SpotifyExporterError("Spotify pagination response contained a non-list items field.")
        debug(f"Fetched page with {len(items)} items")
        for item in items:
            yield item
        raw_next = data.get("next")
        if raw_next in (None, ""):
            next_url = None
        else:
            if not isinstance(raw_next, str):
                raise SpotifyExporterError("Spotify pagination response contained a non-string next field.")
            parsed_next = urlparse(raw_next)
            if not parsed_next.scheme or not parsed_next.netloc:
                raise SpotifyExporterError(f"Spotify pagination returned an invalid next URL: {raw_next}")
            if parsed_next.netloc != api_netloc:
                raise SpotifyExporterError(f"Spotify pagination returned an unexpected next host: {raw_next}")
            next_url = raw_next
        next_params = None


def normalize_playlist_id(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("https://") or raw.startswith("http://"):
        parsed = urlparse(raw)
        parts = parsed.path.rstrip("/").split("/")
        try:
            idx = parts.index("playlist")
            return parts[idx + 1]
        except (ValueError, IndexError):
            raise SpotifyExporterError("Could not parse playlist ID from URL.")
    if raw.startswith("spotify:playlist:"):
        return raw.split(":")[-1]
    return raw


def playlist_track_from_item(item: dict) -> dict:
    track = item.get("item") or item.get("track") or {}
    if track.get("type") == "track":
        return track
    return {}


def get_current_user(token: str) -> dict:
    return api_get("/me", token)


def fetch_playlist_rows(token: str, playlist_id: str) -> tuple[dict, list[dict]]:
    debug(f"Fetching playlist metadata and items for {playlist_id}")
    playlist = api_get(f"/playlists/{playlist_id}", token)
    rows = []
    for item in paginate(f"/playlists/{playlist_id}/items", token, params={"limit": 100}):
        track = playlist_track_from_item(item)
        if not track:
            continue
        artists = ", ".join(artist.get("name", "") for artist in track.get("artists", []))
        rows.append(
            {
                "track_name": track.get("name", ""),
                "artists": artists,
                "track_id": track.get("id", ""),
                "album": (track.get("album") or {}).get("name", ""),
                "track_number": track.get("track_number", ""),
                "disc_number": track.get("disc_number", ""),
                "duration_ms": track.get("duration_ms", ""),
                "explicit": track.get("explicit", False),
                "popularity": track.get("popularity", ""),
                "added_at": item.get("added_at", ""),
                "added_by": (item.get("added_by") or {}).get("id", ""),
                "spotify_url": ((track.get("external_urls") or {}).get("spotify", "")),
            }
        )
    debug(f"Fetched {len(rows)} track rows for playlist {playlist_id}")
    return playlist, rows


def select_rows(rows: list[dict], minimal: bool) -> list[dict]:
    if not minimal:
        return rows
    return [{field: row.get(field, "") for field in MINIMAL_FIELDS} for row in rows]


def write_csv(output_path: Path, rows: list[dict]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else MINIMAL_FIELDS
    write_csv_with_fields(output_path, fieldnames, rows)


def write_csv_with_fields(output_path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    debug(f"Writing CSV to {output_path} with {len(rows)} rows")
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def read_csv_rows(input_path: Path) -> list[dict]:
    debug(f"Reading CSV from {input_path}")
    with input_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    debug(f"Read {len(rows)} rows from {input_path}")
    return rows


def write_txt(output_path: Path, rows: list[dict], separator: str = " - ") -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    debug(f"Writing TXT to {output_path} with {len(rows)} rows")
    with output_path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(f"{row['track_name']}{separator}{row['artists']}\n")


def list_playlists(token: str) -> list[dict]:
    results = []
    for item in paginate("/me/playlists", token, params={"limit": 50}):
        playlist_items = item.get("items") or item.get("tracks") or {}
        playlist_id = item.get("id", "")
        results.append(
            {
                "name": item.get("name", ""),
                "id": playlist_id,
                "owner": (item.get("owner") or {}).get("display_name", ""),
                "owner_id": (item.get("owner") or {}).get("id", ""),
                "tracks_total": playlist_items.get("total", 0),
                "public": item.get("public"),
                "spotify_url": ((item.get("external_urls") or {}).get("spotify", "")),
                "share_url": build_playlist_share_url(playlist_id),
            }
        )
    debug(f"Listed {len(results)} playlists")
    return results


def build_playlist_share_url(playlist_id: str, share_token: str | None = None) -> str:
    if not playlist_id:
        return ""
    share_token = DEFAULT_SHARE_TOKEN if share_token is None else share_token
    base_url = f"https://open.spotify.com/playlist/{playlist_id}"
    if not share_token:
        return base_url
    return f"{base_url}?si={share_token}"


def safe_filename(value: str, fallback: str) -> str:
    normalized = sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("._-")
    return normalized[:80] or fallback


def write_index_csv(output_path: Path, exported: list[dict]) -> None:
    write_csv_with_fields(output_path, OWNED_INDEX_FIELDS, exported)


def dedupe_playlists(playlists: list[dict]) -> list[dict]:
    deduped = []
    seen_playlist_ids = set()
    for playlist in playlists:
        playlist_id = playlist.get("id")
        if not playlist_id or playlist_id in seen_playlist_ids:
            continue
        deduped.append(playlist)
        seen_playlist_ids.add(playlist_id)
    return deduped


def write_playlist_metadata_csv(output_path: Path, playlists: list[dict]) -> None:
    write_csv_with_fields(output_path, PLAYLIST_METADATA_FIELDS, playlists)


def create_chosic_queue(input_path: Path, output_path: Path) -> dict:
    rows = read_csv_rows(input_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    queue_rows = []
    for row in rows:
        playlist_id = row.get("id", "")
        queue_rows.append(
            {
                "status": "todo",
                "name": row.get("name", ""),
                "id": playlist_id,
                "tracks_total": row.get("tracks_total", ""),
                "owner": row.get("owner", ""),
                "share_url": row.get("share_url") or build_playlist_share_url(playlist_id),
                "export_file": "",
                "notes": "",
            }
        )

    write_csv_with_fields(output_path, CHOSIC_QUEUE_FIELDS, queue_rows)

    return {
        "input": str(input_path),
        "output": str(output_path),
        "queued_playlists": len(queue_rows),
    }


def compare_owned_snapshot_indexes(old_path: Path, new_path: Path) -> dict:
    debug(f"Comparing owned snapshot indexes: {old_path} -> {new_path}")
    old_rows = {row.get("id", ""): row for row in read_csv_rows(old_path)}
    new_rows = {row.get("id", ""): row for row in read_csv_rows(new_path)}
    old_ids = {playlist_id for playlist_id in old_rows if playlist_id}
    new_ids = {playlist_id for playlist_id in new_rows if playlist_id}

    added = []
    for playlist_id in sorted(new_ids - old_ids, key=lambda value: new_rows[value].get("name", "").lower()):
        row = new_rows[playlist_id]
        added.append(
            {
                "name": row.get("name", ""),
                "id": playlist_id,
                "tracks": int(row.get("tracks", 0) or 0),
                "output": row.get("output", ""),
            }
        )

    removed = []
    for playlist_id in sorted(old_ids - new_ids, key=lambda value: old_rows[value].get("name", "").lower()):
        row = old_rows[playlist_id]
        removed.append(
            {
                "name": row.get("name", ""),
                "id": playlist_id,
                "tracks": int(row.get("tracks", 0) or 0),
                "output": row.get("output", ""),
            }
        )

    changed = []
    for playlist_id in sorted(old_ids & new_ids, key=lambda value: new_rows[value].get("name", "").lower()):
        old_row = old_rows[playlist_id]
        new_row = new_rows[playlist_id]
        old_tracks = int(old_row.get("tracks", 0) or 0)
        new_tracks = int(new_row.get("tracks", 0) or 0)
        if old_tracks == new_tracks:
            continue
        changed.append(
            {
                "name": new_row.get("name", old_row.get("name", "")),
                "id": playlist_id,
                "old_tracks": old_tracks,
                "new_tracks": new_tracks,
                "delta": new_tracks - old_tracks,
            }
        )

    return {
        "old_input": str(old_path),
        "new_input": str(new_path),
        "old_count": len(old_ids),
        "new_count": len(new_ids),
        "added_count": len(added),
        "removed_count": len(removed),
        "changed_count": len(changed),
        "added": added,
        "removed": removed,
        "changed": changed,
    }


def compare_non_owned_metadata_snapshots(old_path: Path, new_path: Path, ignore_share_url: bool = True) -> dict:
    debug(f"Comparing non-owned metadata snapshots: {old_path} -> {new_path}")
    old_rows = {row.get("id", ""): row for row in read_csv_rows(old_path)}
    new_rows = {row.get("id", ""): row for row in read_csv_rows(new_path)}
    old_ids = {playlist_id for playlist_id in old_rows if playlist_id}
    new_ids = {playlist_id for playlist_id in new_rows if playlist_id}

    added = []
    for playlist_id in sorted(new_ids - old_ids, key=lambda value: new_rows[value].get("name", "").lower()):
        row = new_rows[playlist_id]
        added.append(
            {
                "name": row.get("name", ""),
                "id": playlist_id,
                "owner": row.get("owner", ""),
                "owner_id": row.get("owner_id", ""),
                "tracks_total": int(row.get("tracks_total", 0) or 0),
                "public": row.get("public", ""),
                "spotify_url": row.get("spotify_url", ""),
                "share_url": row.get("share_url", ""),
            }
        )

    removed = []
    for playlist_id in sorted(old_ids - new_ids, key=lambda value: old_rows[value].get("name", "").lower()):
        row = old_rows[playlist_id]
        removed.append(
            {
                "name": row.get("name", ""),
                "id": playlist_id,
                "owner": row.get("owner", ""),
                "owner_id": row.get("owner_id", ""),
                "tracks_total": int(row.get("tracks_total", 0) or 0),
                "public": row.get("public", ""),
                "spotify_url": row.get("spotify_url", ""),
                "share_url": row.get("share_url", ""),
            }
        )

    fields_to_compare = ["name", "owner", "owner_id", "tracks_total", "public", "spotify_url"]
    if not ignore_share_url:
        fields_to_compare.append("share_url")

    changed = []
    for playlist_id in sorted(old_ids & new_ids, key=lambda value: new_rows[value].get("name", "").lower()):
        old_row = old_rows[playlist_id]
        new_row = new_rows[playlist_id]
        diffs = []
        for field in fields_to_compare:
            old_value = old_row.get(field, "")
            new_value = new_row.get(field, "")
            if old_value == new_value:
                continue
            if field == "tracks_total":
                try:
                    old_value = int(old_value or 0)
                    new_value = int(new_value or 0)
                except ValueError:
                    pass
            diffs.append({"field": field, "old": old_value, "new": new_value})
        if not diffs:
            continue
        changed.append(
            {
                "name": new_row.get("name", old_row.get("name", "")),
                "id": playlist_id,
                "diffs": diffs,
            }
        )

    return {
        "old_input": str(old_path),
        "new_input": str(new_path),
        "old_count": len(old_ids),
        "new_count": len(new_ids),
        "added_count": len(added),
        "removed_count": len(removed),
        "changed_count": len(changed),
        "ignore_share_url": ignore_share_url,
        "added": added,
        "removed": removed,
        "changed": changed,
    }


def diff_snapshots(
    old_owned_index: Path | None,
    new_owned_index: Path | None,
    old_non_owned: Path | None,
    new_non_owned: Path | None,
    ignore_share_url: bool = True,
) -> dict:
    if not any([old_owned_index and new_owned_index, old_non_owned and new_non_owned]):
        raise SpotifyExporterError(
            "Provide either both owned index paths, both non-owned metadata paths, or both sets."
        )

    if bool(old_owned_index) != bool(new_owned_index):
        raise SpotifyExporterError("Owned snapshot comparison requires both --old-owned-index and --new-owned-index.")
    if bool(old_non_owned) != bool(new_non_owned):
        raise SpotifyExporterError("Non-owned snapshot comparison requires both --old-non-owned and --new-non-owned.")

    report: dict = {}
    if old_owned_index and new_owned_index:
        report["owned"] = compare_owned_snapshot_indexes(old_owned_index, new_owned_index)
    if old_non_owned and new_non_owned:
        report["non_owned"] = compare_non_owned_metadata_snapshots(
            old_non_owned,
            new_non_owned,
            ignore_share_url=ignore_share_url,
        )
    return report


def write_duplicate_tracks_csv(output_path: Path, playlist_rows: list[tuple[dict, list[dict]]]) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    by_track_id: dict[str, dict] = {}
    for playlist, rows in playlist_rows:
        for row in rows:
            track_id = row.get("track_id", "")
            if not track_id:
                continue
            entry = by_track_id.setdefault(
                track_id,
                {
                    "track_id": track_id,
                    "track_name": row.get("track_name", ""),
                    "artists": row.get("artists", ""),
                    "spotify_url": row.get("spotify_url", ""),
                    "playlist_ids": set(),
                    "playlist_names": set(),
                },
            )
            entry["playlist_ids"].add(playlist.get("id", ""))
            entry["playlist_names"].add(playlist.get("name", ""))

    duplicate_rows = []
    for entry in by_track_id.values():
        playlist_ids = sorted(value for value in entry["playlist_ids"] if value)
        if len(playlist_ids) < 2:
            continue
        playlist_names = sorted(value for value in entry["playlist_names"] if value)
        duplicate_rows.append(
            {
                "track_id": entry["track_id"],
                "track_name": entry["track_name"],
                "artists": entry["artists"],
                "playlist_count": len(playlist_ids),
                "playlist_names": " | ".join(playlist_names),
                "playlist_ids": " | ".join(playlist_ids),
                "spotify_url": entry["spotify_url"],
            }
        )

    duplicate_rows.sort(key=lambda row: (-row["playlist_count"], row["artists"], row["track_name"]))
    fieldnames = [
        "track_id",
        "track_name",
        "artists",
        "playlist_count",
        "playlist_names",
        "playlist_ids",
        "spotify_url",
    ]
    write_csv_with_fields(output_path, fieldnames, duplicate_rows)
    return len(duplicate_rows)


def export_owned_playlists(
    token: str,
    output_dir: Path,
    minimal: bool = False,
    continue_on_error: bool = False,
) -> dict:
    current_user = get_current_user(token)
    current_user_id = current_user.get("id", "")
    if not current_user_id:
        raise SpotifyExporterError("Could not determine current Spotify user ID.")

    output_dir.mkdir(parents=True, exist_ok=True)
    playlists = list_playlists(token)
    owned_playlists = [
        playlist for playlist in dedupe_playlists(playlists) if playlist.get("owner_id") == current_user_id
    ]
    summary = {
        "current_user_id": current_user_id,
        "seen_playlists": len(playlists),
        "owned_playlists": len(owned_playlists),
        "index_output": str(output_dir / "index.csv"),
        "duplicate_tracks_output": str(output_dir / "duplicate_tracks.csv"),
        "exported": [],
        "failed": [],
    }

    used_paths: set[Path] = set()
    exported_playlist_rows: list[tuple[dict, list[dict]]] = []
    for playlist in owned_playlists:
        playlist_id = playlist["id"]
        filename_base = safe_filename(playlist.get("name", ""), playlist_id)
        output_path = output_dir / f"{filename_base}_{playlist_id}.csv"
        duplicate_index = 2
        while output_path in used_paths:
            output_path = output_dir / f"{filename_base}_{playlist_id}_{duplicate_index}.csv"
            duplicate_index += 1
        used_paths.add(output_path)

        try:
            _, rows = fetch_playlist_rows(token, playlist_id)
            exported_playlist_rows.append((playlist, rows))
            output_rows = select_rows(rows, minimal=minimal)
            write_csv(output_path, output_rows)
            summary["exported"].append(
                {
                    "name": playlist.get("name", ""),
                    "id": playlist_id,
                    "tracks": len(output_rows),
                    "output": str(output_path),
                }
            )
        except SpotifyExporterError as exc:
            failure = {"name": playlist.get("name", ""), "id": playlist_id, "error": str(exc)}
            summary["failed"].append(failure)
            if not continue_on_error:
                raise SpotifyExporterError(f"Failed exporting playlist {playlist_id}: {exc}")

    write_index_csv(output_dir / "index.csv", summary["exported"])
    summary["duplicate_tracks"] = write_duplicate_tracks_csv(
        output_dir / "duplicate_tracks.csv",
        exported_playlist_rows,
    )
    return summary


def export_non_owned_playlist_metadata(token: str, output_path: Path) -> dict:
    current_user = get_current_user(token)
    current_user_id = current_user.get("id", "")
    if not current_user_id:
        raise SpotifyExporterError("Could not determine current Spotify user ID.")

    playlists = list_playlists(token)
    non_owned_playlists = [
        playlist for playlist in dedupe_playlists(playlists) if playlist.get("owner_id") != current_user_id
    ]
    write_playlist_metadata_csv(output_path, non_owned_playlists)
    return {
        "current_user_id": current_user_id,
        "seen_playlists": len(playlists),
        "non_owned_playlists": len(non_owned_playlists),
        "output": str(output_path),
    }


def response_summary(response: requests.Response) -> dict:
    summary: dict = {
        "status_code": response.status_code,
        "ok": response.ok,
    }
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        summary["retry_after"] = retry_after
    try:
        payload = response.json()
    except ValueError:
        summary["body"] = response.text[:500]
        return summary
    if response.ok:
        return summary
    summary["error"] = payload.get("error", payload)
    return summary


def diagnose_playlist(token: str, playlist_id: str, sample_size: int = 5) -> dict:
    metadata_response = api_get_response(f"/playlists/{playlist_id}", token)
    report: dict = {
        "playlist_id": playlist_id,
        "metadata": response_summary(metadata_response),
    }
    if metadata_response.ok:
        metadata = metadata_response.json()
        playlist_items = metadata.get("items") or metadata.get("tracks") or {}
        report["playlist"] = {
            "name": metadata.get("name", ""),
            "owner_id": (metadata.get("owner") or {}).get("id", ""),
            "collaborative": metadata.get("collaborative"),
            "public": metadata.get("public"),
            "items_total": playlist_items.get("total"),
            "has_items_field": "items" in metadata,
            "has_tracks_field": "tracks" in metadata,
        }

    items_response = api_get_response(f"/playlists/{playlist_id}/items", token, params={"limit": sample_size})
    report["items"] = response_summary(items_response)
    if items_response.ok:
        payload = items_response.json()
        entries = payload.get("items", [])
        shape_counts = {"item": 0, "track": 0, "other": 0}
        track_count = 0
        for entry in entries:
            if "item" in entry:
                shape_counts["item"] += 1
            elif "track" in entry:
                shape_counts["track"] += 1
            else:
                shape_counts["other"] += 1
            if playlist_track_from_item(entry):
                track_count += 1
        report["items"].update(
            {
                "returned_count": len(entries),
                "total": payload.get("total"),
                "limit": payload.get("limit"),
                "next": payload.get("next"),
                "shape_counts": shape_counts,
                "track_entries": track_count,
            }
        )
    return report


class CallbackHandler(BaseHTTPRequestHandler):
    server_version = "SpotifyExporterCallback/1.0"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not found")
            return

        params = parse_qs(parsed.query)
        self.server.auth_result = {k: v[0] for k, v in params.items()}  # type: ignore[attr-defined]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h1>Spotify auth complete.</h1><p>You can close this tab and return to the terminal.</p></body></html>"
        )

    def log_message(self, format, *args):
        return


def run_auth_server(timeout_seconds: int, host: str, port: int) -> dict:
    debug(f"Starting callback server on {host}:{port} with timeout {timeout_seconds}s")
    httpd = HTTPServer((host, port), CallbackHandler)
    httpd.timeout = 0.5
    httpd.auth_result = None  # type: ignore[attr-defined]

    def serve():
        while httpd.auth_result is None:  # type: ignore[attr-defined]
            httpd.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()

    try:
        started = time.time()
        while time.time() - started < timeout_seconds:
            result = httpd.auth_result  # type: ignore[attr-defined]
            if result is not None:
                debug("Received OAuth callback")
                return result
            time.sleep(0.25)
    finally:
        httpd.server_close()

    raise SpotifyExporterError("Timed out waiting for Spotify callback.")


def command_auth(args: argparse.Namespace) -> None:
    require_requests()
    client_id = load_env("SPOTIFY_CLIENT_ID")
    client_secret = load_env("SPOTIFY_CLIENT_SECRET")
    redirect_uri = os.environ.get("SPOTIFY_REDIRECT_URI", DEFAULT_REDIRECT_URI)
    redirect_port = int(urlparse(redirect_uri).port or 8888)
    if args.port != redirect_port:
        raise SpotifyExporterError(
            f"Callback port {args.port} must match SPOTIFY_REDIRECT_URI port {redirect_port}."
        )
    auth_url = build_auth_url(client_id, redirect_uri, SPOTIFY_SCOPES)
    debug(f"Using redirect URI {redirect_uri}")
    print("Open this URL in your browser and approve access:\n")
    print(auth_url)
    print()

    if args.open_browser:
        webbrowser.open(auth_url)

    callback = run_auth_server(timeout_seconds=args.timeout, host=args.host, port=args.port)
    if "error" in callback:
        raise SpotifyExporterError(f"Authorization failed: {callback['error']}")

    expected_state = STATE_CACHE.read_text(encoding="utf-8").strip() if STATE_CACHE.exists() else ""
    if callback.get("state") != expected_state:
        raise SpotifyExporterError("State mismatch during OAuth callback.")

    exchange_code_for_token(client_id, client_secret, redirect_uri, callback["code"])
    print(f"Access token cached at {TOKEN_CACHE}")


def command_export(args: argparse.Namespace) -> None:
    require_requests()
    client_id = load_env("SPOTIFY_CLIENT_ID")
    client_secret = load_env("SPOTIFY_CLIENT_SECRET")
    token = get_valid_token(client_id, client_secret)
    playlist_id = normalize_playlist_id(args.playlist)
    playlist, rows = fetch_playlist_rows(token, playlist_id)
    rows = select_rows(rows, minimal=args.minimal)

    output = Path(args.output)
    if args.format == "csv":
        write_csv(output, rows)
    else:
        write_txt(output, rows, separator=args.separator)

    print(f"Exported {len(rows)} tracks from '{playlist.get('name', playlist_id)}' to {output}")


def command_export_owned(args: argparse.Namespace) -> None:
    require_requests()
    client_id = load_env("SPOTIFY_CLIENT_ID")
    client_secret = load_env("SPOTIFY_CLIENT_SECRET")
    token = get_valid_token(client_id, client_secret)
    summary = export_owned_playlists(
        token,
        Path(args.output_dir),
        minimal=args.minimal,
        continue_on_error=args.continue_on_error,
    )
    print(json.dumps(summary, indent=2))


def command_export_non_owned_metadata(args: argparse.Namespace) -> None:
    require_requests()
    client_id = load_env("SPOTIFY_CLIENT_ID")
    client_secret = load_env("SPOTIFY_CLIENT_SECRET")
    token = get_valid_token(client_id, client_secret)
    summary = export_non_owned_playlist_metadata(token, Path(args.output))
    print(json.dumps(summary, indent=2))


def command_create_chosic_queue(args: argparse.Namespace) -> None:
    summary = create_chosic_queue(Path(args.input), Path(args.output))
    print(json.dumps(summary, indent=2))


def command_diff_snapshots(args: argparse.Namespace) -> None:
    report = diff_snapshots(
        Path(args.old_owned_index) if args.old_owned_index else None,
        Path(args.new_owned_index) if args.new_owned_index else None,
        Path(args.old_non_owned) if args.old_non_owned else None,
        Path(args.new_non_owned) if args.new_non_owned else None,
        ignore_share_url=not args.include_share_url,
    )
    print(json.dumps(report, indent=2))


def command_list_playlists(args: argparse.Namespace) -> None:
    require_requests()
    client_id = load_env("SPOTIFY_CLIENT_ID")
    client_secret = load_env("SPOTIFY_CLIENT_SECRET")
    token = get_valid_token(client_id, client_secret)
    playlists = list_playlists(token)

    if args.format == "json":
        print(json.dumps(playlists, indent=2))
        return

    writer = csv.DictWriter(
        sys.stdout,
        fieldnames=PLAYLIST_METADATA_FIELDS,
    )
    writer.writeheader()
    writer.writerows(playlists)


def command_diagnose_playlist(args: argparse.Namespace) -> None:
    require_requests()
    client_id = load_env("SPOTIFY_CLIENT_ID")
    client_secret = load_env("SPOTIFY_CLIENT_SECRET")
    token = get_valid_token(client_id, client_secret)
    playlist_id = normalize_playlist_id(args.playlist)
    report = diagnose_playlist(token, playlist_id, sample_size=args.sample_size)
    print(json.dumps(report, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export Spotify playlists to CSV or TXT.")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {load_version()}",
    )
    parser.add_argument(
        "--verbose",
        "--debug",
        action="store_true",
        dest="verbose",
        help="Print diagnostic progress information to stderr.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth_parser = subparsers.add_parser("auth", help="Run interactive Spotify authorization flow.")
    auth_parser.add_argument(
        "--host",
        default=DEFAULT_CALLBACK_HOST,
        help="Callback server bind host. Use 0.0.0.0 for Docker port publishing.",
    )
    auth_parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Callback server port.")
    auth_parser.add_argument("--timeout", type=int, default=180, help="Seconds to wait for the callback.")
    auth_parser.add_argument("--open-browser", action="store_true", help="Try to open the authorization URL automatically.")
    auth_parser.set_defaults(func=command_auth)

    export_parser = subparsers.add_parser("export", help="Export a playlist to CSV or TXT.")
    export_parser.add_argument("--playlist", required=True, help="Playlist URL, URI, or raw playlist ID.")
    export_parser.add_argument("--format", choices=["csv", "txt"], default="csv")
    export_parser.add_argument("--output", required=True, help="Output file path.")
    export_parser.add_argument(
        "--minimal",
        action="store_true",
        help="Export only track_name and artists.",
    )
    export_parser.add_argument(
        "--separator",
        default=" - ",
        help="Separator for TXT output. Ignored for CSV.",
    )
    export_parser.set_defaults(func=command_export)

    export_owned_parser = subparsers.add_parser(
        "export-owned",
        help="Export each playlist owned by the authenticated user to a separate CSV.",
    )
    export_owned_parser.add_argument("--output-dir", required=True, help="Directory where CSV files are written.")
    export_owned_parser.add_argument(
        "--minimal",
        action="store_true",
        help="Export only track_name and artists.",
    )
    export_owned_parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue exporting remaining playlists if one playlist fails.",
    )
    export_owned_parser.set_defaults(func=command_export_owned)

    export_non_owned_parser = subparsers.add_parser(
        "export-non-owned-metadata",
        help="Export visible non-owned playlist metadata to one CSV without fetching item rows.",
    )
    export_non_owned_parser.add_argument("--output", required=True, help="Output CSV file path.")
    export_non_owned_parser.set_defaults(func=command_export_non_owned_metadata)

    chosic_parser = subparsers.add_parser(
        "create-chosic-queue",
        help="Create a manual processing queue CSV from non-owned playlist metadata.",
    )
    chosic_parser.add_argument("--input", required=True, help="Input non-owned playlist metadata CSV.")
    chosic_parser.add_argument("--output", required=True, help="Output queue CSV file path.")
    chosic_parser.set_defaults(func=command_create_chosic_queue)

    diff_parser = subparsers.add_parser(
        "diff-snapshots",
        help="Compare owned index CSVs and/or non-owned metadata CSVs from two export snapshots.",
    )
    diff_parser.add_argument("--old-owned-index", help="Older owned-playlists index.csv path.")
    diff_parser.add_argument("--new-owned-index", help="Newer owned-playlists index.csv path.")
    diff_parser.add_argument("--old-non-owned", help="Older non-owned playlist metadata CSV path.")
    diff_parser.add_argument("--new-non-owned", help="Newer non-owned playlist metadata CSV path.")
    diff_parser.add_argument(
        "--include-share-url",
        action="store_true",
        help="Include share_url changes in non-owned snapshot diffs.",
    )
    diff_parser.set_defaults(func=command_diff_snapshots)

    list_parser = subparsers.add_parser("list-playlists", help="List current user's playlists.")
    list_parser.add_argument("--format", choices=["csv", "json"], default="csv")
    list_parser.set_defaults(func=command_list_playlists)

    diagnose_parser = subparsers.add_parser(
        "diagnose-playlist",
        help="Inspect Spotify API responses for a playlist under the current app/account mode.",
    )
    diagnose_parser.add_argument("--playlist", required=True, help="Playlist URL, URI, or raw playlist ID.")
    diagnose_parser.add_argument(
        "--sample-size",
        type=int,
        default=5,
        help="Number of playlist items to request for response-shape inspection.",
    )
    diagnose_parser.set_defaults(func=command_diagnose_playlist)

    return parser


def main() -> int:
    try:
        parser = build_parser()
        args = parser.parse_args()
        set_verbose(args.verbose)
        args.func(args)
        return 0
    except SpotifyExporterError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ERROR: File operation failed: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"ERROR: Failed to parse response data: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
