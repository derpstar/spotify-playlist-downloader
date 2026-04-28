# Spotify Playlist Exporter (Dockerized)

This tiny containerized utility exports Spotify playlist **track/artist lists** to CSV or TXT. It does **not** download audio.

## What it does

- Runs a local OAuth callback so you can log into Spotify in your browser.
- Caches your access + refresh token in a mounted `/data` volume.
- Lists your playlists.
- Exports one playlist to:
  - `csv` (full metadata by default)
  - `csv --minimal` (just `track_name,artists`)
  - `txt` (`Song - Artist` per line by default)

## Files

- `app.py` — CLI entrypoint
- `Dockerfile` — container image build
- `requirements.txt` — Python dependencies
- `.env.example` — environment variable template
- `IMPLEMENTATION_NOTES.md` — design and implementation notes

## Prerequisites

1. Open the Spotify Developer Dashboard and create an app.
2. Enable/use Spotify Web API access for the app.
3. In the app settings, add this exact Redirect URI:

```text
http://127.0.0.1:8888/callback
```

4. Copy `.env.example` to `.env` and fill in the Client ID and Client Secret from the dashboard:

```env
SPOTIFY_CLIENT_ID=your_spotify_app_client_id
SPOTIFY_CLIENT_SECRET=your_spotify_app_client_secret
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
SPOTIFY_CALLBACK_HOST=0.0.0.0
SPOTIFY_SHARE_TOKEN=local-export
```

5. If the app is in Development Mode, make sure the Spotify account you authenticate with is allowed to use the app in the dashboard.

Development Mode apps can list playlists visible to the authenticated account, but Spotify may only return playlist item rows for playlists the account owns or collaborates on. This tool's `export-owned` command is designed around that restriction.

`SPOTIFY_REDIRECT_URI` is the browser-facing URI registered with Spotify. When running in Docker, keep it as `http://127.0.0.1:8888/callback` and publish the same port with `-p 8888:8888`.

`SPOTIFY_CALLBACK_HOST` controls the address the in-container callback server binds to. The default `0.0.0.0` is required for Docker port publishing. If you run `app.py` directly on your host machine and only want a loopback listener, set it to `127.0.0.1`.

`SPOTIFY_SHARE_TOKEN` is optional. It only controls the `si=` marker added to generated playlist share URLs. It is not an auth token and can be left as `local-export` or set to an empty value to produce bare Spotify playlist URLs.

Never commit `.env`, `data/`, `output/`, or saved browser pages. They can contain local credentials, OAuth tokens, or personal playlist data, and this repo ignores them by default.

## Build

```bash
docker build -t spotify-playlist-exporter .
```

## Windows / WSL2

This project works on Windows through WSL2 with Docker Desktop.

Recommended setup:

1. Install Docker Desktop on Windows.
2. Enable WSL2 integration for your Linux distro in Docker Desktop settings.
3. Clone the repo inside your WSL home directory, for example under `~/code/`, instead of under `/mnt/c/`.
4. Run all commands from the WSL shell.
5. Keep the Spotify Redirect URI set to:

```text
http://127.0.0.1:8888/callback
```

Notes:

- The existing Docker commands in this README work in WSL as written.
- Volume mounts such as `-v "$PWD/data:/data"` and `-v "$PWD/output:/output"` resolve correctly from the WSL shell.
- `auth --open-browser` may open your default Windows browser. If it does not, copy the printed authorization URL from the terminal and open it manually.
- Port publishing with `-p 8888:8888` should still use `127.0.0.1:8888` in the browser on the Windows side.
- If file performance feels slow, move the repo out of `/mnt/c/...` and into the Linux filesystem inside WSL.

## Authenticate

```bash
docker run --rm -it \
  --env-file .env \
  -p 8888:8888 \
  -v "$PWD/data:/data" \
  spotify-playlist-exporter auth --open-browser
```

This caches tokens in `./data/token.json`.

If you change the callback port, update both `SPOTIFY_REDIRECT_URI` and the Docker `-p host:container` mapping. The app will reject auth startup when the configured callback port and redirect URI port do not match.

## List playlists

```bash
docker run --rm -it \
  --env-file .env \
  -v "$PWD/data:/data" \
  spotify-playlist-exporter list-playlists
```

## Diagnose playlist access

Use this when checking whether Spotify returns item rows for a playlist under your current app mode.

```bash
docker run --rm -it \
  --env-file .env \
  -v "$PWD/data:/data" \
  spotify-playlist-exporter diagnose-playlist \
  --playlist "https://open.spotify.com/playlist/PUT_ID_HERE"
```

The command prints JSON with metadata endpoint status, playlist item endpoint status, returned item count, and whether Spotify used the current `item` response shape or the legacy `track` shape.

## Export a playlist

### Full CSV

```bash
docker run --rm -it \
  --env-file .env \
  -v "$PWD/data:/data" \
  -v "$PWD/output:/output" \
  spotify-playlist-exporter export \
  --playlist "https://open.spotify.com/playlist/PUT_ID_HERE" \
  --format csv \
  --output /output/playlist.csv
```

### Minimal CSV (track + artist only)

```bash
docker run --rm -it \
  --env-file .env \
  -v "$PWD/data:/data" \
  -v "$PWD/output:/output" \
  spotify-playlist-exporter export \
  --playlist "https://open.spotify.com/playlist/PUT_ID_HERE" \
  --format csv \
  --minimal \
  --output /output/playlist_minimal.csv
```

### TXT with a custom separator

```bash
docker run --rm -it \
  --env-file .env \
  -v "$PWD/data:/data" \
  -v "$PWD/output:/output" \
  spotify-playlist-exporter export \
  --playlist "spotify:playlist:PUT_ID_HERE" \
  --format txt \
  --separator " — " \
  --output /output/playlist.txt
```

## Export owned playlists

This writes one CSV per playlist owned by the authenticated Spotify user.

```bash
docker run --rm -it \
  --env-file .env \
  -v "$PWD/data:/data" \
  -v "$PWD/output:/output" \
  spotify-playlist-exporter export-owned \
  --output-dir /output/owned-playlists \
  --continue-on-error
```

Each playlist file is named from the playlist name and ID. The command also writes:

- `index.csv` with one row per exported playlist.
- `duplicate_tracks.csv` with tracks that appear in more than one exported playlist.

The command prints a JSON summary of exported playlists, failures, and duplicate-track count.

## Export non-owned playlist metadata

This writes one CSV containing metadata for visible playlists that are not owned by the authenticated Spotify user. It does not fetch playlist item rows.

```bash
docker run --rm -it \
  --env-file .env \
  -v "$PWD/data:/data" \
  -v "$PWD/output:/output" \
  spotify-playlist-exporter export-non-owned-metadata \
  --output /output/non-owned-playlists.csv
```

The CSV includes playlist name, ID, owner, owner ID, track total, public flag, Spotify URL, and a share-style URL.

If Spotify only returns metadata for a non-owned playlist and you still need a track list, one manual fallback is https://www.chosic.com/spotify-playlist-exporter/ . The `share_url` column in this export is intended to make that workflow easier.

## Create Chosic queue

This creates a manual processing queue from the non-owned playlist metadata CSV.

```bash
docker run --rm -it \
  -v "$PWD/output:/output" \
  spotify-playlist-exporter create-chosic-queue \
  --input /output/non-owned-playlists.csv \
  --output /output/chosic_queue.csv
```

The queue CSV includes `status`, `share_url`, `export_file`, and `notes` columns so playlists can be marked `todo`, `done`, or `failed` while processing them through Chosic manually.

## Compare snapshots

This compares two owned-playlist index files and/or two non-owned metadata CSVs and prints a JSON diff report.

```bash
docker run --rm -it \
  -v "$PWD/output:/output" \
  spotify-playlist-exporter diff-snapshots \
  --old-owned-index /output/owned-playlists-report/index.csv \
  --new-owned-index /output/owned-playlists-2026-04-27/index.csv \
  --old-non-owned /output/non-owned-playlists.csv \
  --new-non-owned /output/non-owned-playlists-2026-04-27.csv
```

By default, `share_url` changes are ignored for non-owned diffs so the report focuses on real playlist changes. Add `--include-share-url` if you want those changes included.

## Notes

- Supported playlist input forms:
  - Playlist URL
  - Spotify playlist URI
  - Raw playlist ID
- TXT exports always use `track_name` and `artists`.
- `--separator` only affects TXT output.
- `--minimal` trims CSV output to just `track_name` and `artists`.
- Spotify Development Mode apps may only return playlist contents for playlists the authenticated user owns or collaborates on. For other playlists, Spotify may return metadata without item rows.
- For non-owned playlists where Spotify only returns metadata, a manual browser workflow with https://www.chosic.com/spotify-playlist-exporter/ can be used with the exported `share_url` values.
- Network requests use bounded timeouts. OAuth waits up to 180 seconds by default and can be changed with `auth --timeout`.
