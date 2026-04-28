# Implementation Notes

## Purpose

This project exports Spotify playlist contents as **metadata only**. It is intentionally limited to song and artist listings plus optional playlist metadata. It does not attempt to download, mirror, or transform audio.

## Architecture

The exporter is packaged as a small Python CLI inside a Docker container.

### Main components

- `app.py` contains the entire command-line workflow.
- `Dockerfile` builds a minimal Python image and sets the CLI as the container entrypoint.
- `/data` is a mounted volume used to persist OAuth tokens across container runs.
- `/output` is an optional mounted volume where exports are written.

## Authentication approach

The implementation uses Spotify's **Authorization Code** flow because the tool needs access to the authenticated user's playlists.

### Why this flow

Client Credentials is not sufficient for user-specific playlist access. The exporter needs playlist scopes tied to the signed-in Spotify account, so it starts a temporary local callback server and completes the browser login flow.

### Callback handling

- The tool generates a random OAuth state token and stores it in `/data/oauth_state.txt`.
- It starts a small HTTP server bound to the callback port.
- The bind host is configurable with `SPOTIFY_CALLBACK_HOST`; Docker runs should use `0.0.0.0` so the published port can reach the in-container server.
- After the user signs in, Spotify redirects to the local callback URI.
- The tool validates the returned state value before exchanging the authorization code for tokens.

## Token persistence and refresh

The access token and refresh token are cached in `/data/token.json`.

At runtime:
- If the cached access token is still valid, the tool uses it directly.
- If the access token is close to expiry, the tool refreshes it automatically with the stored refresh token.
- The refreshed token payload overwrites the cached token file.

Token and OAuth state cache files are written with private file permissions where the host filesystem supports them.

This keeps the login friction low after the first successful authentication.

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

This is useful when the goal is simply a clean list of songs and artists for sharing, archiving, or copying into a spreadsheet.

## TXT formatting

TXT output always renders one line per row using:

```text
track_name + separator + artists
```

The default separator is:

```text
 - 
```

This can be overridden with `--separator`, which makes it easier to produce cleaner human-readable lists.

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

These are printed cleanly to stderr and returned with a non-zero exit code.

All Spotify HTTP calls use a 30 second request timeout. The local OAuth callback server polls with a short socket timeout and the overall auth wait defaults to 180 seconds.

## Containerization notes

The container is intentionally simple:

- Base image: `python:3.12-slim`
- Single dependency: `requests`
- Entry point: `python /app/app.py`

This keeps the image small and easy to rebuild. It also makes the project easy to adapt into a larger automation workflow later.

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

When Spotify only returns metadata for non-owned playlists, the exporter can still produce share-style URLs. Those can be used in a manual browser workflow with https://www.chosic.com/spotify-playlist-exporter/ to retrieve track listings outside the Spotify Web API path.

### Port mapping

During `auth`, the container port must be published so the local Spotify callback can reach the in-container HTTP server.

Example:

```bash
-p 8888:8888
```

## Public release hygiene

The tracked `.gitignore` and `.dockerignore` exclude local credentials, OAuth tokens, exports, saved browser pages, bytecode, and the local offline HTML experiment script from source control and Docker build context.

## Extension ideas

Possible next additions:

- `--playlist-name` selection without needing to copy IDs manually
- batch export for all playlists
- JSON export mode
- deduplication mode
- sort options such as artist-first or album-first
- direct spreadsheet-friendly output presets

## Design tradeoffs

This implementation keeps everything in a single file for ease of handoff and portability. That is convenient for a small utility, though a larger version would likely split auth, Spotify API access, and formatting into separate modules.

The current design is a good fit for:
- local personal use
- containerized automation
- simple scripting and export workflows
- easy future extension
