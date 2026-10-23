#!/usr/bin/env python3
"""Export direct Legacy Games installer URLs for games in YOUR account.

Derived from the public Heirloom Games Manager API implementation:
https://github.com/HeirloomGM/heirloom-gm

Python 3.9+; standard library only. Credentials are prompted for and never saved.
"""

from __future__ import annotations

import argparse
import base64
import csv
import getpass
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, unquote, urlsplit
from urllib.request import Request, urlopen

API_BASE = "https://api.legacygames.com"
COLUMNS = ["title", "source", "game_id", "product_id", "installer_uuid",
           "filename", "download_url", "reported_size", "status", "bytes", "sha256", "error"]


def api_get(endpoint, auth, params=None):
    url = API_BASE + endpoint
    if params:
        url += "?" + urlencode(params)
    request = Request(url, headers={
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "?token?",  # Matches the Legacy Games Launcher API client.
        "usertoken": "Basic " + auth,
        "User-Agent": "LegacyGamesPersonalBackup/1.0",
    })
    try:
        with urlopen(request, timeout=40) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Legacy Games API returned HTTP {exc.code} for {endpoint}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cannot reach Legacy Games API ({endpoint}): {exc.reason}") from exc


def items(value):
    return value if isinstance(value, list) else []


def extract_owned_games(auth):
    login = api_get("/users/login", auth)
    user_id = (login.get("data") or {}).get("userId") if isinstance(login, dict) else None
    if not user_id:
        raise RuntimeError("Login failed: no user ID returned. Check your email/password.")

    # The store's catalog maps purchases to the individual games in each pack.
    catalog = items(api_get("/products/catalog", auth))
    receipts = api_get("/users/downloads", auth, {"userId": user_id})
    purchases = items((receipts or {}).get("data"))
    purchased_ids = {str(p["product_id"]) for p in purchases
                     if isinstance(p, dict) and p.get("product_id") is not None}

    games = []
    seen = set()
    for product in catalog:
        if not isinstance(product, dict) or str(product.get("product_id")) not in purchased_ids:
            continue
        for game in items(product.get("games")):
            if not isinstance(game, dict):
                continue
            name = str(game.get("game_name") or "").strip()
            if not name:
                continue
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            games.append({
                "title": name, "source": "purchased", "game_id": str(game.get("game_id") or ""),
                "product_id": str(product.get("product_id") or ""),
                "installer_uuid": str(game.get("installer_uuid") or ""),
                "reported_size": str(game.get("game_installed_size") or ""),
            })

    # Prime/Luna giveaways are associated with the account's confirmed email.
    profile = api_get("/users/profile", auth, {"userId": user_id})
    email = (profile.get("data") or {}).get("email") if isinstance(profile, dict) else None
    if not email:
        print("Warning: no email in profile; could not read Prime/Luna giveaways.", file=sys.stderr)
        return games

    giveaways = api_get("/users/getgiveawaycatalogbyemail", auth, {"email": email})
    for pack in items((giveaways or {}).get("data")):
        if not isinstance(pack, dict):
            continue
        for game in items(pack.get("games")):
            if not isinstance(game, dict):
                continue
            name = str(game.get("game_name") or "").strip()
            if not name or name.casefold() in seen:
                continue
            seen.add(name.casefold())
            games.append({
                "title": name, "source": "giveaway", "game_id": str(game.get("game_id") or ""),
                "product_id": "", "installer_uuid": str(game.get("installer_uuid") or ""),
                "reported_size": str(game.get("game_installed_size") or ""),
            })
    return games


def filename_from_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Installer response did not contain a valid HTTPS link")
    filename = unquote(parsed.path.rsplit("/", 1)[-1])
    if not filename or not filename.lower().endswith(".exe"):
        raise ValueError("Installer link does not point to an .exe file")
    # Only use the server-supplied last path component; keep it safe on Windows.
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).rstrip(" .")


def resolve_links(games, auth, delay):
    resolved = []
    total = len(games)
    for i, game in enumerate(games, 1):
        entry = {key: "" for key in COLUMNS}
        entry.update(game)
        try:
            if game["source"] == "giveaway":
                if not game["installer_uuid"]:
                    raise ValueError("Missing installer_uuid in giveaway catalog")
                resp = api_get("/products/giveawaydownload", auth, {"installerUuid": game["installer_uuid"]})
            else:
                if not game["game_id"] or not game["product_id"]:
                    raise ValueError("Missing product ID or game ID")
                resp = api_get("/products/download", auth, {
                    "productId": game["product_id"], "gameId": game["game_id"]})
            data = resp.get("data") if isinstance(resp, dict) else None
            url = data.get("file") if isinstance(data, dict) else None
            if not isinstance(url, str) or not url:
                raise ValueError("No download URL returned by the Legacy Games API")
            entry["filename"] = filename_from_url(url)
            entry["download_url"] = url
            entry["status"] = "link found"
            print(f"[{i}/{total}] {game['title']}: {entry['filename']}")
        except (RuntimeError, ValueError, KeyError, TypeError, TimeoutError) as exc:
            entry["status"] = "failed"
            entry["error"] = str(exc)
            print(f"[{i}/{total}] {game['title']}: ERROR: {exc}", file=sys.stderr)
        resolved.append(entry)
        if delay and i < total:
            time.sleep(delay)
    return resolved


def write_exports(rows, output):
    output.mkdir(parents=True, exist_ok=True)
    with (output / "legacygames_links.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    with (output / "legacygames_urls.txt").open("w", encoding="utf-8") as stream:
        for row in rows:
            if row["download_url"]:
                stream.write(row["download_url"] + "\n")


def human_size(number):
    """Format bytes using binary units, e.g. 245.3 MiB."""
    number = float(number)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(number) < 1024 or unit == "TiB":
            return f"{number:.0f} {unit}" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024


class ProgressMeter:
    """An updating terminal bar; infrequent full lines when output is redirected."""

    def __init__(self, starting_bytes, total_bytes):
        self.start = time.monotonic()
        self.starting_bytes = starting_bytes
        self.total_bytes = total_bytes
        self.last_update = 0
        self.tty = sys.stdout.isatty()
        self.width = 0
        self.shown = False

    def update(self, current, force=False):
        now = time.monotonic()
        if not force and now - self.last_update < (0.15 if self.tty else 5):
            return
        self.last_update = now
        elapsed = max(now - self.start, 0.001)
        speed = max(0, current - self.starting_bytes) / elapsed
        if self.total_bytes is not None and self.total_bytes > 0:
            percentage = min(100, 100 * current / self.total_bytes)
            blocks = min(26, int(26 * percentage / 100))
            bar = "#" * blocks + "-" * (26 - blocks)
            line = (f"  [{bar}] {percentage:5.1f}%  "
                    f"{human_size(current)} / {human_size(self.total_bytes)}  "
                    f"{human_size(speed)}/s")
        else:
            line = f"  {human_size(current)} / unknown total  {human_size(speed)}/s"
        if self.tty:
            self.width = max(self.width, len(line))
            print("\r" + line.ljust(self.width), end="", flush=True)
        else:
            print(line, flush=True)
        self.shown = True

    def close(self):
        if self.tty and self.shown:
            print()
            self.shown = False


def url_identity(url):
    """Ignore expiring query tokens when identifying an installer for resuming."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def get_partial_metadata(path):
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read resume metadata {path}: {exc}") from exc


def save_partial_metadata(path, data):
    temporary = Path(str(path) + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
    os.replace(temporary, path)


def complete_download(row, partial, destination, metadata_path, resumed):
    os.replace(partial, destination)
    metadata_path.unlink(missing_ok=True)
    row["bytes"] = destination.stat().st_size
    print("  Calculating SHA-256 checksum...")
    row["sha256"] = sha256_file(destination)
    row["status"] = "downloaded (resumed)" if resumed else "downloaded"
    row["error"] = ""


def download_one(row, directory):
    name = row["filename"]
    destination = directory / name
    if destination.is_file() and destination.stat().st_size > 0:
        # Never overwrite an existing final installer automatically.
        row["status"] = "already exists (not reverified)"
        row["bytes"] = destination.stat().st_size
        row["sha256"] = sha256_file(destination)
        row["error"] = ""
        print(f"  Already saved: {human_size(destination.stat().st_size)} (skipped)")
        return

    partial = directory / (name + ".part")
    metadata_path = directory / (name + ".part.json")
    prior = partial.stat().st_size if partial.is_file() else 0
    old_metadata = get_partial_metadata(metadata_path) if prior else {}
    identity = url_identity(row["download_url"])
    if prior and old_metadata.get("url_identity") not in (None, identity):
        raise RuntimeError(
            f"Existing {partial.name} belongs to a different URL. "
            "Move the .part and .part.json files out of this folder before restarting."
        )
    if prior:
        print(f"  Found unfinished download: {human_size(prior)}; requesting remaining bytes...")

    # If the range is unsatisfiable, the saved .part may already be complete;
    # otherwise retry the request without Range and safely start again.
    for attempt in range(2):
        offset = prior if attempt == 0 else 0
        headers = {"User-Agent": "LegacyGamesPersonalBackup/1.1", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
            validator = old_metadata.get("etag")
            if isinstance(validator, str) and validator and not validator.startswith("W/"):
                headers["If-Range"] = validator
            elif old_metadata.get("last_modified"):
                headers["If-Range"] = old_metadata["last_modified"]
        request = Request(row["download_url"], headers=headers)
        try:
            response = urlopen(request, timeout=60)
        except HTTPError as exc:
            if exc.code == 416 and offset:
                content_range = exc.headers.get("Content-Range", "")
                matched = re.fullmatch(r"bytes\s+\*/(\d+)", content_range.strip())
                if matched and int(matched.group(1)) == prior:
                    meter = ProgressMeter(prior, prior)
                    meter.update(prior, force=True)
                    meter.close()
                    complete_download(row, partial, destination, metadata_path, resumed=True)
                    return
                print("  Saved range cannot be used; restarting this installer from zero.")
                continue
            raise

        with response:
            status = getattr(response, "status", None) or response.getcode()
            if status not in (200, 206):
                raise RuntimeError(f"Unexpected HTTP status {status} for {name}")
            response_type = response.headers.get("Content-Type", "").lower()
            if "text/html" in response_type:
                raise RuntimeError("The download URL returned an HTML page, not an installer")

            length_header = response.headers.get("Content-Length")
            content_length = int(length_header) if length_header is not None else None
            if content_length is not None and content_length < 0:
                raise RuntimeError("Invalid Content-Length from CDN")
            resumable = status == 206 and offset > 0
            start_byte = 0
            expected_total = content_length
            if status == 206:
                header = response.headers.get("Content-Range", "")
                match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", header.strip())
                if not match:
                    raise RuntimeError("CDN returned 206 without a valid Content-Range")
                first, last = int(match.group(1)), int(match.group(2))
                expected_total = int(match.group(3)) if match.group(3) != "*" else None
                if first != offset or last < first or (expected_total is not None and last >= expected_total):
                    raise RuntimeError("CDN returned an unexpected resume byte range")
                if content_length is not None and content_length != last - first + 1:
                    raise RuntimeError("Inconsistent CDN byte range and Content-Length")
                start_byte = first
                if expected_total is None and content_length is not None:
                    expected_total = first + content_length
                if resumable:
                    if (old_metadata.get("total_bytes") is not None and expected_total is not None
                            and int(old_metadata["total_bytes"]) != expected_total):
                        raise RuntimeError("Remote installer size changed; cannot safely resume")
                    for local_key, remote_key in (("etag", "ETag"), ("last_modified", "Last-Modified")):
                        old_value = old_metadata.get(local_key)
                        new_value = response.headers.get(remote_key)
                        if old_value and new_value and old_value != new_value:
                            raise RuntimeError("Installer changed on CDN; cannot safely resume")
            elif offset:
                print("  Server did not support the requested range; restarting from zero.")

            info = {
                "url_identity": identity,
                "etag": response.headers.get("ETag") or old_metadata.get("etag"),
                "last_modified": response.headers.get("Last-Modified") or old_metadata.get("last_modified"),
                "total_bytes": expected_total,
            }
            # If restarting, no stale validator may survive into the next attempt.
            if not resumable:
                info["etag"] = response.headers.get("ETag")
                info["last_modified"] = response.headers.get("Last-Modified")
            save_partial_metadata(metadata_path, info)
            meter = ProgressMeter(start_byte, expected_total)
            current = start_byte
            try:
                meter.update(current, force=True)
                with partial.open("ab" if resumable else "wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        current += len(chunk)
                        meter.update(current)
                meter.update(current, force=True)
            finally:
                meter.close()
            if expected_total is not None and current != expected_total:
                raise RuntimeError(
                    f"Incomplete: received {human_size(current)} / {human_size(expected_total)}. "
                    "The .part file was kept for the next run."
                )
            complete_download(row, partial, destination, metadata_path, resumed=resumable)
            return

    raise RuntimeError("Could not restart download after the CDN rejected the saved byte range")


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_saved_links(output):
    saved = output / "legacygames_links.csv"
    if not saved.exists():
        raise RuntimeError(f"No saved links at {saved}. Run with --download first.")
    with saved.open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or "download_url" not in reader.fieldnames:
            raise RuntimeError(f"Missing download_url column in {saved}")
        return [{column: row.get(column, "") for column in COLUMNS}
                for row in reader]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export direct installer links for the Legacy Games in your account")
    parser.add_argument("--output", default="LegacyGamesBackup", help="Backup directory (default: ./LegacyGamesBackup)")
    parser.add_argument("--download", action="store_true", help="Also save the .exe installers")
    parser.add_argument("--resume", action="store_true", help="Continue downloads from an existing backup CSV without logging in")
    parser.add_argument("--delay", type=float, default=0.2, help="Seconds between API link requests (default: 0.2)")
    args = parser.parse_args(argv)
    if args.delay < 0:
        parser.error("--delay cannot be negative")
    out = Path(args.output).expanduser().resolve()
    try:
        if args.resume:
            rows = read_saved_links(out)
            print(f"Loaded {len(rows)} saved games from {out / 'legacygames_links.csv'}")
            print("Using saved URLs. If they have expired, rerun with --download to refresh them.")
        else:
            email = input("Legacy Games email / username: ").strip()
            if not email:
                parser.error("An account email or username is required")
            password = getpass.getpass("Legacy Games password (hidden, never saved): ")
            if not password:
                parser.error("A password is required for this API")
            auth = base64.b64encode(f"{email}:{password}".encode("utf-8")).decode("ascii")
            del password
            games = extract_owned_games(auth)
            print(f"Found {len(games)} owned games (including any Prime/Luna giveaways).")
            if not games:
                print("No games found. Check whether your purchases and giveaways are linked to this account.")
                return 1
            rows = resolve_links(games, auth, args.delay)
            write_exports(rows, out)
            good = sum(bool(r["download_url"]) for r in rows)
            print(f"Saved {good}/{len(rows)} installer URLs to {out / 'legacygames_urls.txt'}")
            print(f"Saved game names, sources and any errors to {out / 'legacygames_links.csv'}")

        if args.download or args.resume:
            installers = out / "installers"
            installers.mkdir(parents=True, exist_ok=True)
            for i, row in enumerate(rows, 1):
                if not row["download_url"]:
                    continue
                print(f"Downloading [{i}/{len(rows)}]: {row['title']} ({row['filename']})")
                try:
                    download_one(row, installers)
                except KeyboardInterrupt:
                    row["status"] = "interrupted (resumable)"
                    row["error"] = "Interrupted by user; rerun --resume or --download"
                    write_exports(rows, out)
                    print("\nStopped. Partial files are saved; use --resume to continue.", file=sys.stderr)
                    return 130
                except (HTTPError, URLError, OSError, RuntimeError, ValueError, TimeoutError) as exc:
                    row["status"] = "download failed"
                    row["error"] = str(exc)
                    print(f"Download failed: {exc}", file=sys.stderr)
                write_exports(rows, out)  # Preserve status after each installer.
            completed = [r for r in rows if r["status"].startswith(("downloaded", "already exists"))]
            total_saved = sum(int(r["bytes"] or 0) for r in completed)
            print(f"Installers saved: {len(completed)}; total size: {human_size(total_saved)}")
        failures = [r for r in rows if r["status"] in ("failed", "download failed")]
        if failures:
            print(f"WARNING: {len(failures)} failed. Review the 'status' and 'error' CSV columns.", file=sys.stderr)
            return 2
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted. Rerun to continue.", file=sys.stderr)
        return 130
    except (RuntimeError, ValueError, HTTPError, URLError, OSError, TimeoutError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
