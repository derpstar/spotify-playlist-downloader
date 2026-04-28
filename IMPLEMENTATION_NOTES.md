# Implementation Notes

## Purpose

This tool exports personally created and shared ownership Spotify playlist contents as **metadata only**. It is intentionally limited to song and artist listings plus optional playlist metadata. It does not attempt to download, mirror, or transform audio.

## Architecture

The tool is packaged as a small Python CLI inside a Docker container for portability, see the project README for further details.

### Main components

- `app.py` contains the entire command-line workflow.
- `Dockerfile` builds a minimal Python image and sets the CLI as the container entrypoint.
- `/data` is a mounted volume used to persist OAuth tokens across container runs.
- `/output` is an optional mounted volume where exports are written.

## Authentication

The tool uses Spotify's **Authorization Code** flow to access the authenticated user's playlists.

### Why this workflow

Client Credentials are not sufficient for playlist access on Spotify, so we start a temporary local callback server to complete the browser login.

### Callback handling

- This script generates a random OAuth state token and stores it in `/data/oauth_state.txt`.
- Also starts a small HTTP server bound to the callback port.
- The bind host is configurable with `SPOTIFY_CALLBACK_HOST`; Docker runs should use `0.0.0.0` so the published port can reach the in-container server.
- After user sign-in, Spotify redirects to the local callback URI.
- Validate the returned state value before exchanging the authorization code for tokens.

## Token persistence and refresh

The access token and refresh token are cached in `/data/token.json`.

At runtime:
- If the cached access token is still valid, the tool uses it directly.
- If the access token is close to expiry, the tool refreshes it automatically with the stored refresh token.
- The refreshed token payload overwrites the cached token file.

Token and OAuth state cache files are written with private file permissions where the host filesystem supports them.

## Playlist retrieval

Playlist export happens in two steps:

1. Fetch playlist metadata from `/playlists/{playlist_id}`
2. Fetch playlist items from `/playlists/{playlist_id}/items`

The track endpoint is paginated, so the exporter follows Spotify's `next` links until all playlist items are collected.

Spotify's 2026 Development Mode changes renamed playlist item payload fields from `track` to `item` and replaced the old `/playlists/{id}/tracks` endpoint with `/playlists/{id}/items`. The exporter reads the new shape first and keeps fallback parsing for older or Extended Quota Mode responses.

## Output model

The exporter builds one normalized row per playlist item.

### Full export fields

- `track_name`
- `artists`
- `album`
- `track_number`
- `disc_number`
- `duration_ms`
- `explicit`
- `popularity`
- `added_at`
- `added_by`
- `spotify_url`

### Minimal mode

When `--minimal` is used, the exporter trims each row to:

- `track_name`
- `artists`

## TXT formatting

TXT output always renders one line per row using:

```text
track_name + separator + artists
```

The default separator is:

```text
 - 
```

This can be overridden with `--separator`.

## Error handling

The CLI raises a custom `SpotifyExporterError` for expected user-facing failures, such as:

- missing environment variables
- invalid or missing token cache
- playlist ID parsing failures
- token exchange failures
- Spotify API request failures
- OAuth timeout or state mismatch
- filesystem failures when reading inputs or writing outputs
- malformed JSON responses from Spotify

These are printed to stderr and returned with a non-zero exit code.

All Spotify HTTP calls use a 30 second request timeout. The local OAuth callback server polls with a short socket timeout and the overall auth wait defaults to 180 seconds.

## Containerization notes

simple container image:

- Base image: `python:3.12-slim`
- Single dependency: `requests`
- Entry point: `python /app/app.py`

## Operational notes

### Required environment variables

- `SPOTIFY_CLIENT_ID`
- `SPOTIFY_CLIENT_SECRET`
- `SPOTIFY_REDIRECT_URI` (optional if using the default callback)
- `SPOTIFY_CALLBACK_HOST` (optional, defaults to `0.0.0.0` for Docker)
- `SPOTIFY_SHARE_TOKEN` (optional, defaults to `local-export`; controls generated playlist URL `si=` markers only)

### Volumes

- Mount `/data` to preserve login state
- Mount an output directory when writing export files

### Non-owned playlist fallback

Spotify only returns metadata for non-owned playlists due to API limitations. This tool will produce share-style URLs which can be used to export non-owned playlists using services such as https://www.chosic.com/spotify-playlist-exporter/

### Port mapping

During `auth`, the container port must be published so the local Spotify callback can reach the in-container HTTP server.

Example:

```bash
-p 8888:8888
```

## Future add-ons

Nice-to-haves:

- `--playlist-name` selection without needing to copy IDs manually
- batch export for all playlists
- JSON export mode
- deduplication mode
- sort options such as artist-first or album-first
- direct spreadsheet-friendly output presets

## LLM disclosure
An LLM was used to generate initial documentation, unit tests and troubleshooting.
