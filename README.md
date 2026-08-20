# 12-Provider IPTV M3U Auto Updater

A clean GitHub Actions workflow inspired by the supplied `jio.zip` project.

## What it does

- Fetches 12 configured provider sources every 30 minutes.
- Produces one M3U file per provider under `playlists/`.
- Converts the three JSON providers into M3U.
- Validates existing M3U providers before replacing their files.
- Preserves source M3U metadata/comments, including KodiProp, EXTVLCOPT and EXTHTTP lines.
- Preserves cookies, tokens, URLs and other provider metadata present in the source.
- A failed/empty/invalid provider never overwrites its last known-good playlist.
- Commits only files that actually changed.
- A failure of one provider does not stop the other providers.

## Provider outputs

| Provider | Output |
|---|---|
| Yashiscool JioTV | `playlists/yashiscool-jiotv.m3u` |
| SixPG JioTV | `playlists/sixpg-jiotv.m3u` |
| Sayan JioTV | `playlists/sayan-jiotv.m3u` |
| StreamStar JioTV | `playlists/streamstar-jiotv.m3u` |
| Alex JioTV | `playlists/alex-jiotv.m3u` |
| STBPLUS JioTV | `playlists/stbplus-jiotv.m3u` |
| STBPLUS JioTV Mobile | `playlists/stbplus-jiotv-mobile.m3u` |
| Star Sports | `playlists/star-sports.m3u` |
| GeoPlus | `playlists/geoplus.m3u` |
| Hotstar | `playlists/hotstar.m3u` |
| Voot | `playlists/voot.m3u` |
| Sony | `playlists/sony.m3u` |

## Run manually

GitHub → Actions → `Update IPTV Provider Playlists` → `Run workflow`.

The scheduled job runs at `*/30 * * * *` (every 30 minutes, UTC).

## Important

The workflow preserves provider-supplied playback metadata. M3U directives such as `#KODIPROP`, `#EXTVLCOPT`, and `#EXTHTTP` are conventions supported differently by different IPTV players; no single M3U dialect can guarantee identical DRM/header behavior in every player.

