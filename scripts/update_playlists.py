#!/usr/bin/env python3
"""
12-provider M3U updater.

Design goals:
- A provider is updated independently.
- Failed/empty/invalid providers never overwrite their previous playlist.
- M3U source metadata is preserved instead of being unnecessarily rewritten.
- JSON sources are converted to the same M3U/KodiProp-style format used by
  the original project where the source scripts explicitly supplied those fields.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "playlists"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TIMEOUT = (15, 45)
MAX_REDIRECTS = 5
MIN_CHANNELS = 1

COMMON_HEADERS = {
    "User-Agent": "OTT Navigator",
    "Accept": "*/*",
}


@dataclass(frozen=True)
class Provider:
    name: str
    output: str
    url: str
    kind: str
    parser: Callable[[str], str]


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def xmlish_escape(value: str) -> str:
    # EXTINF attributes are quoted. Avoid breaking the attribute syntax.
    return value.replace("\\", "\\\\").replace('"', "'").replace("\r", " ").replace("\n", " ")


def json_header_line(headers: dict[str, str]) -> str:
    return "#EXTHTTP:" + json.dumps(headers, ensure_ascii=False, separators=(",", ":"))


def request_text(url: str, headers: dict[str, str] | None = None) -> str:
    session = requests.Session()
    session.max_redirects = MAX_REDIRECTS

    response = session.get(
        url,
        headers={**COMMON_HEADERS, **(headers or {})},
        timeout=TIMEOUT,
        allow_redirects=True,
    )
    response.raise_for_status()

    content = response.content
    if not content:
        raise ValueError("empty HTTP response")

    # utf-8-sig handles a BOM without leaving it in the playlist.
    return content.decode("utf-8-sig", errors="replace")


def extract_m3u_entries(content: str) -> list[str]:
    """
    Validate the basic M3U structure and return complete channel blocks.

    A block begins with #EXTINF and must contain a following non-comment
    stream URL. All metadata lines between EXTINF and the URL are retained.
    """
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    # Allow a UTF-8 BOM and blank lines before #EXTM3U.
    stripped = [line.strip() for line in lines if line.strip()]
    if not stripped:
        raise ValueError("empty playlist")

    if stripped[0].upper() != "#EXTM3U":
        # Some providers omit the header. We can safely normalize that case
        # only if the body still contains valid EXTINF entries.
        if not any(line.startswith("#EXTINF:") for line in stripped):
            raise ValueError("response is not an M3U playlist")

    entries: list[str] = []
    current: list[str] | None = None
    has_url = False

    for raw in lines:
        line = raw.strip()

        if line.startswith("#EXTINF:"):
            if current is not None and has_url:
                entries.append("\n".join(current).strip())

            current = [line]
            has_url = False
            continue

        if current is None:
            continue

        if not line:
            continue

        current.append(line)

        if not line.startswith("#"):
            has_url = True
            entries.append("\n".join(current).strip())
            current = None
            has_url = False

    if current is not None and has_url:
        entries.append("\n".join(current).strip())

    if len(entries) < MIN_CHANNELS:
        raise ValueError(f"playlist contains only {len(entries)} valid channel(s)")

    # Extra validation: every entry must have EXTINF and a URL.
    valid: list[str] = []
    for entry in entries:
        entry_lines = entry.splitlines()
        if not entry_lines or not entry_lines[0].startswith("#EXTINF:"):
            continue
        if not any(not x.startswith("#") and x.strip() for x in entry_lines[1:]):
            continue
        valid.append(entry)

    if len(valid) < MIN_CHANNELS:
        raise ValueError("no complete M3U channel entries found")

    return valid


def normalize_m3u(content: str) -> str:
    entries = extract_m3u_entries(content)
    # Preserve every metadata line in every valid source entry.
    return "#EXTM3U\n\n" + "\n\n".join(entries) + "\n"


def value_from(obj: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = obj.get(key)
        if value is not None and clean_text(value):
            return clean_text(value)
    return ""


def normalize_json_root(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        channels = data
    elif isinstance(data, dict):
        if isinstance(data.get("channels"), list):
            channels = data["channels"]
        elif isinstance(data.get("data"), list):
            channels = data["data"]
        else:
            raise ValueError("JSON object does not contain a channel list")
    else:
        raise ValueError("JSON root must be a list or object containing channels")

    result = [x for x in channels if isinstance(x, dict)]
    if len(result) < MIN_CHANNELS:
        raise ValueError("JSON contains no channel objects")
    return result


def build_json_m3u(
    channels: list[dict[str, Any]],
    *,
    default_group: str,
    user_agent: str,
    origin: str | None = None,
    referer: str | None = None,
    filter_func: Callable[[dict[str, Any]], bool] | None = None,
) -> str:
    lines = ["#EXTM3U"]
    written = 0

    for ch in channels:
        if filter_func and not filter_func(ch):
            continue

        channel_id = value_from(ch, "id", "channel_id", "channelId")
        name = value_from(ch, "name", "channel_name", "title") or "Unknown"
        logo = value_from(ch, "logo", "tvg_logo", "image", "image_url")
        group = value_from(ch, "category", "group", "group-title", "group_title") or default_group

        stream = value_from(
            ch,
            "stream_url",
            "streamUrl",
            "mpd",
            "mpd_url",
            "url",
            "stream",
            "playback_url",
        )

        cookie = value_from(ch, "cookie", "cookies")
        key_id = value_from(ch, "key_id", "keyId", "kid", "keyID")
        key = value_from(ch, "key", "key_value")

        if not stream:
            continue

        license_key = f"{key_id}:{key}" if key_id and key else ""

        lines.append(
            f'#EXTINF:-1 tvg-id="{xmlish_escape(channel_id)}" '
            f'tvg-name="{xmlish_escape(name)}" '
            f'tvg-logo="{xmlish_escape(logo)}" '
            f'group-title="{xmlish_escape(group)}",{name}'
        )

        # Match the adaptive/Kodi metadata pattern used by the source
        # converters in the supplied project.
        if stream.lower().split("?", 1)[0].endswith(".mpd") or "mpd" in stream.lower():
            lines.append("#KODIPROP:inputstream=inputstream.adaptive")
            lines.append("#KODIPROP:inputstream.adaptive.manifest_type=mpd")

            if license_key:
                lines.append("#KODIPROP:inputstream.adaptive.license_type=clearkey")
                lines.append(
                    f"#KODIPROP:inputstream.adaptive.license_key={license_key}"
                )

        if user_agent:
            lines.append(f"#EXTVLCOPT:http-user-agent={user_agent}")

        headers: dict[str, str] = {}
        if cookie:
            headers["cookie"] = cookie
        if origin:
            headers["Origin"] = origin
        if referer:
            headers["Referer"] = referer

        # Preserve provider JSON header fields if they exist.
        source_headers = ch.get("headers")
        if isinstance(source_headers, dict):
            for k, v in source_headers.items():
                if clean_text(v):
                    headers[str(k)] = clean_text(v)

        if headers:
            lines.append(json_header_line(headers))

        lines.append(stream)
        lines.append("")
        written += 1

    if written < MIN_CHANNELS:
        raise ValueError("JSON provider produced no usable channels")

    return "\n".join(lines).rstrip() + "\n"


def fetch_m3u_provider(url: str) -> str:
    content = request_text(url)
    return normalize_m3u(content)


def fetch_yashiscool(url: str) -> str:
    raw = request_text(url)
    data = json.loads(raw)
    channels = normalize_json_root(data)

    return build_json_m3u(
        channels,
        default_group="English",
        user_agent="Sayan10",
        origin=None,
        referer=None,
    )


def fetch_star_sports(url: str) -> str:
    raw = request_text(url)
    data = json.loads(raw)

    if not isinstance(data, dict) or not isinstance(data.get("channels"), list):
        raise ValueError("Star Sports JSON must contain a 'channels' list")

    channels = [x for x in data["channels"] if isinstance(x, dict)]

    return build_json_m3u(
        channels,
        default_group="Sports",
        user_agent="Sayan10",
        origin="https://www.jiotv.com/",
        referer="https://www.jiotv.com/",
    )


def fetch_geoplus(url: str) -> str:
    raw = request_text(url)
    data = json.loads(raw)

    if not isinstance(data, list):
        raise ValueError("GeoPlus JSON must be a list")

    channels = [x for x in data if isinstance(x, dict)]

    # GeoPlus entries use mpd/keyId/key/cookie/logo/category in the supplied
    # project. build_json_m3u maps those exact source fields.
    return build_json_m3u(
        channels,
        default_group="Sports",
        user_agent="Sayan10",
        origin="https://www.jiotv.com/",
        referer="https://www.jiotv.com/",
    )


PROVIDERS = [
    Provider(
        "Yashiscool JioTV",
        "jtv1.m3u",
        "https://raw.githubusercontent.com/yashiscool123/TV-/refs/heads/main/jtv.json",
        "json",
        fetch_yashiscool,
    ),
    Provider(
        "SixPG JioTV",
        "jtv2.m3u",
        "https://raw.githubusercontent.com/sixpg/zeyo-test/refs/heads/main/jtv.m3u",
        "m3u",
        fetch_m3u_provider,
    ),
    Provider(
        "Sayan JioTV",
        "jtv3.m3u",
        "https://sayan-jiotv.spal75084.workers.dev/jtv.m3u",
        "m3u",
        fetch_m3u_provider,
    ),
    Provider(
        "StreamStar JioTV",
        "jtv4.m3u",
        "https://mute-sunset-8225.streamstar18.workers.dev/",
        "m3u",
        fetch_m3u_provider,
    ),
    Provider(
        "Alex JioTV",
        "jtv5.m3u",
        "https://raw.githubusercontent.com/alex4528y/m3u/refs/heads/main/jtv.m3u",
        "m3u",
        fetch_m3u_provider,
    ),
    Provider(
        "STBPLUS JioTV",
        "jtv6.m3u",
        "https://raw.githubusercontent.com/Sflex0719/STBPLUS/refs/heads/main/Zio.m3u",
        "m3u",
        fetch_m3u_provider,
    ),
    Provider(
        "STBPLUS JioTV Mobile",
        "jtv7.m3u",
        "https://raw.githubusercontent.com/Sflex0719/STBPLUS/refs/heads/main/ZioMobile.m3u",
        "m3u",
        fetch_m3u_provider,
    ),
    Provider(
        "Star Sports",
        "s-sports.m3u",
        "https://sonujson-v3.pages.dev/Data/sports.json",
        "json",
        "fetch_star_sports",
    ),
    Provider(
        "GeoPlus",
        "jtv+1.m3u",
        "https://raw.githubusercontent.com/qwerty180506/json/refs/heads/main/Geoplus.json",
        "json",
        "fetch_geoplus",
    ),
    Provider(
        "Hotstar",
        "hotstar.m3u",
        "https://myhotstarapi.bmera5952.workers.dev/?playlist=1",
        "m3u",
        fetch_m3u_provider,
    ),
    Provider(
        "Voot",
        "voot.m3u",
        "https://voot.vodep39240327.workers.dev/?voot.m3u",
        "m3u",
        fetch_m3u_provider,
    ),
    Provider(
        "Sony",
        "sony.m3u",
        "https://json.cloudplay.qzz.io/all-files-raw/sony.m3u",
        "m3u",
        fetch_m3u_provider,
    ),
]


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp:
        tmp.write(content)
        tmp.flush()
        os_fsync = getattr(tmp, "fileno", None)
        if os_fsync:
            import os
            os.fsync(tmp.fileno())
        temp_name = Path(tmp.name)

    temp_name.replace(path)


def validate_generated_m3u(content: str) -> int:
    entries = extract_m3u_entries(content)

    for index, entry in enumerate(entries, start=1):
        first = entry.splitlines()[0]
        if not first.startswith("#EXTINF:"):
            raise ValueError(f"entry {index} has no EXTINF")
        if not any(
            line.strip() and not line.strip().startswith("#")
            for line in entry.splitlines()[1:]
        ):
            raise ValueError(f"entry {index} has no stream URL")

    return len(entries)


def process_provider(provider: Provider) -> tuple[bool, int, str]:
    output_path = OUTPUT_DIR / provider.output

    try:
        print(f"\n=== {provider.name} ===")
        print(f"Source: {provider.url}")

        content = provider.parser(provider.url)
        count = validate_generated_m3u(content)

        if count < MIN_CHANNELS:
            raise ValueError(f"validation returned only {count} channel(s)")

        atomic_write(output_path, content)
        print(f"SUCCESS: {count} channel(s) -> {output_path}")
        return True, count, ""

    except Exception as exc:
        # Crucial behavior: do not touch the old file.
        print(f"FAILED: {provider.name}: {exc}")
        if output_path.exists():
            print(f"UNCHANGED: keeping existing {output_path}")
        else:
            print("NO EXISTING PLAYLIST: nothing to preserve")
        return False, 0, str(exc)


def main() -> int:
    print("12-provider IPTV playlist updater")
    print(f"Output directory: {OUTPUT_DIR}")
    print(f"Providers: {len(PROVIDERS)}")

    success = 0
    failed = 0
    summary: list[tuple[str, bool, int]] = []

    for provider in PROVIDERS:
        ok, count, _ = process_provider(provider)
        summary.append((provider.name, ok, count))
        if ok:
            success += 1
        else:
            failed += 1

    print("\n=== SUMMARY ===")
    for name, ok, count in summary:
        state = "OK" if ok else "FAILED/UNCHANGED"
        print(f"{state:17} {name:28} {count:5} channels")

    print(f"\nSuccessful: {success}/{len(PROVIDERS)}")
    print(f"Failed:     {failed}/{len(PROVIDERS)}")

    # Deliberately return 0 when individual providers fail: one broken source
    # must not prevent successful providers from being committed.
    return 0


if __name__ == "__main__":
    sys.exit(main())
  
