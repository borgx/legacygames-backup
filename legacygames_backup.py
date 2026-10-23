#!/usr/bin/env python3
"""Back up Legacy Games installers and verify their integrity.

Derived from Heirloom Games Manager's API implementation:
https://github.com/HeirloomGM/heirloom-gm
Python 3.9+, standard library only. Credentials are prompted for, never saved.
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
import shutil
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

API_BASE = "https://api.legacygames.com"
OLD_COLUMNS = ["title", "source", "game_id", "product_id", "installer_uuid",
               "filename", "download_url", "reported_size", "status", "bytes", "sha256", "error"]
# Old columns are retained in exactly their original order for CSV compatibility.
COLUMNS = OLD_COLUMNS + ["md5", "cdn_etag", "cdn_bytes", "verification_status"]
MD5_ETAG = re.compile(r"[0-9a-fA-F]{32}\Z")
SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}\Z")
CHUNK_SIZE = 1024 * 1024


def api_get(endpoint, auth, params=None):
    url = API_BASE + endpoint
    if params:
        url += "?" + urlencode(params)
    request = Request(url, headers={
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "?token?",  # Legacy Games Launcher API client header.
        "usertoken": "Basic " + auth,
        "User-Agent": "LegacyGamesPersonalBackup/1.0",
    })
    try:
        with urlopen(request, timeout=40) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Legacy Games API returned HTTP {exc.code} for {endpoint}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cannot reach Legacy Games API ({endpoint})") from exc


def items(value):
    return value if isinstance(value, list) else []


def extract_owned_games(auth):
    login = api_get("/users/login", auth)
    user_id = (login.get("data") or {}).get("userId") if isinstance(login, dict) else None
    if not user_id:
        raise RuntimeError("Login failed: no user ID returned. Check your email/password.")
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
            if not name or name.casefold() in seen:
                continue
            seen.add(name.casefold())
            games.append({
                "title": name, "source": "purchased", "game_id": str(game.get("game_id") or ""),
                "product_id": str(product.get("product_id") or ""),
                "installer_uuid": str(game.get("installer_uuid") or ""),
                "reported_size": str(game.get("game_installed_size") or ""),
            })
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


def https_url(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme.lower() == "https" and bool(parsed.hostname)
                and not parsed.username and not parsed.password)
    except ValueError:
        return False


def filename_from_url(url):
    if not https_url(url):
        raise ValueError("Installer response did not contain a valid HTTPS link")
    filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    if not filename or not filename.lower().endswith(".exe"):
        raise ValueError("Installer link does not point to an .exe file")
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).rstrip(" .")


def valid_filename(filename):
    return (bool(filename) and filename not in (".", "..") and "/" not in filename
            and "\\" not in filename and Path(filename).name == filename
            and filename.lower().endswith(".exe"))


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


def url_identity(url):
    """Ignore expiring query tokens when identifying an installer for resuming."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def merge_prior_verification(rows, previous_rows):
    """Refresh download links without erasing trusted, previously recorded hashes."""
    old_by_key = {}
    for old in previous_rows:
        if old.get("filename"):
            key = (old["source"], old["game_id"], old["product_id"],
                   old["installer_uuid"], old["filename"])
            old_by_key[key] = old
    for row in rows:
        key = (row["source"], row["game_id"], row["product_id"],
               row["installer_uuid"], row["filename"])
        old = old_by_key.get(key)
        if old:
            for field in ("bytes", "sha256", "md5", "cdn_etag", "cdn_bytes", "verification_status"):
                row[field] = old.get(field, "")
    return rows


def write_exports(rows, output, *, urls=True):
    """Atomically replace CSV; retain a one-time .bak of the pre-existing manifest."""
    output.mkdir(parents=True, exist_ok=True)
    saved = output / "legacygames_links.csv"
    backup = output / "legacygames_links.csv.bak"
    if saved.is_file() and not backup.exists():
        shutil.copy2(saved, backup)
    tmp = output / "legacygames_links.csv.tmp"
    try:
        with tmp.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, saved)
    finally:
        tmp.unlink(missing_ok=True)
    if urls:
        with (output / "legacygames_urls.txt").open("w", encoding="utf-8") as stream:
            for row in rows:
                if row.get("download_url"):
                    stream.write(row["download_url"] + "\n")


def read_saved_links(output):
    saved = output / "legacygames_links.csv"
    if not saved.exists():
        raise RuntimeError(f"No saved links at {saved}. Run with --download first.")
    with saved.open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames or "download_url" not in reader.fieldnames:
            raise RuntimeError(f"Missing download_url column in {saved}")
        return [{col: (row.get(col) or "") for col in COLUMNS} for row in reader]


def human_size(number):
    number = float(number)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(number) < 1024 or unit == "TiB":
            return f"{number:.0f} {unit}" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024


class ProgressMeter:
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


def md5_from_etag(etag):
    """Only a strong, single-part, MD5-shaped ETag is a checksum *candidate*."""
    value = (etag or "").strip()
    if value.startswith("W/"):
        return ""
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    return value.lower() if MD5_ETAG.fullmatch(value) else ""


def safe_etag(value):
    """Avoid CSV formula injection or control characters in HTTP metadata."""
    if not isinstance(value, str) or len(value) > 120 or not all(32 <= ord(c) < 127 for c in value):
        return ""
    return value if value[:1] not in ("=", "+", "-", "@") else ""


class HeadRedirectHandler(HTTPRedirectHandler):
    """Follow HTTPS redirects without ever turning HEAD into a GET."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not https_url(newurl):
            return None
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        return Request(newurl, method="HEAD", headers={
            "User-Agent": "LegacyGamesPersonalBackup/1.2", "Accept-Encoding": "identity"})


HEAD_OPENER = build_opener(HeadRedirectHandler())


def get_cdn_head(url):
    """Return (etag, size, error). Never download installer data."""
    if not https_url(url):
        return "", None, "Invalid or non-HTTPS download URL"
    request = Request(url, method="HEAD", headers={
        "User-Agent": "LegacyGamesPersonalBackup/1.2", "Accept-Encoding": "identity"})
    try:
        with HEAD_OPENER.open(request, timeout=30) as response:
            if response.status != 200:
                return "", None, f"CDN HEAD returned HTTP {response.status}"
            headers = response.headers
            if headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                return "", None, "CDN used content encoding"
            if "text/html" in headers.get("Content-Type", "").lower():
                return "", None, "CDN returned HTML instead of installer metadata"
            length = headers.get("Content-Length")
            try:
                size = int(length) if length is not None else None
                if size is not None and size < 0:
                    raise ValueError
            except ValueError:
                return "", None, "Invalid CDN Content-Length"
            return safe_etag(headers.get("ETag", "")), size, ""
    except HTTPError as exc:
        code = exc.code
        exc.close()
        return "", None, f"CDN HEAD returned HTTP {code}"
    except (URLError, OSError, TimeoutError, ValueError):
        # Never print a network exception: it may contain private URL query tokens.
        return "", None, "CDN HEAD unavailable"


def file_hashes(path):
    md5 = hashlib.md5()  # Equality check against CDN ETag, NOT for security.
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            md5.update(chunk)
            sha.update(chunk)
    return md5.hexdigest(), sha.hexdigest()


def _saved_size(value):
    try:
        return int(value) if value else None
    except (ValueError, TypeError, OverflowError):
        return None


def inspect_file(row, path, *, remote_etag="", remote_bytes=None, head_error=""):
    """Compare local bytes with historical manifest and current CDN metadata.

    Does not mutate the saved hash or ETag on a mismatch. Returns status string.
    """
    current_bytes = path.stat().st_size
    md5, sha256 = file_hashes(path)
    discrepancies = []
    old_sha = (row.get("sha256") or "").lower()
    old_md5 = (row.get("md5") or "").lower()
    previous_bytes = _saved_size(row.get("bytes"))
    old_cdn_bytes = _saved_size(row.get("cdn_bytes"))
    if old_sha and (not SHA256_HEX.fullmatch(old_sha) or sha256 != old_sha):
        discrepancies.append("saved SHA-256 differs")
    if old_md5 and (not MD5_ETAG.fullmatch(old_md5) or md5 != old_md5):
        discrepancies.append("saved MD5 differs")
    if previous_bytes is not None and current_bytes != previous_bytes:
        discrepancies.append("saved byte count differs")
    if old_cdn_bytes is not None and current_bytes != old_cdn_bytes and not remote_bytes:
        discrepancies.append("previous CDN byte count differs")
    if remote_bytes is not None and current_bytes != remote_bytes:
        discrepancies.append("current CDN byte count differs")
    candidate = md5_from_etag(remote_etag)
    if candidate and md5 != candidate:
        discrepancies.append("CDN ETag differs from local MD5 (possibly updated remotely)")
    if discrepancies:
        row["verification_status"] = "MISMATCH"
        row["error"] = "; ".join(discrepancies)
        return "MISMATCH"

    # Hashes are persisted *only* after all known checks have succeeded.
    row["bytes"] = str(current_bytes)
    row["md5"] = md5
    if not old_sha:
        row["sha256"] = sha256  # First recorded baseline, not a remote SHA-256.
    if remote_etag:
        row["cdn_etag"] = remote_etag
    if remote_bytes is not None:
        row["cdn_bytes"] = str(remote_bytes)
    if candidate:
        status = "VERIFIED_MD5_ETAG"
    elif old_sha or old_md5:
        status = "LOCAL_SHA256_ONLY" if old_sha else "LOCAL_MD5_ONLY"
    else:
        status = "UNVERIFIED"
    row["verification_status"] = status
    row["error"] = head_error if head_error else ""
    return status


def verify_existing(row, directory, *, local_only=False):
    """Read/hash an existing installer; optionally compare with CDN HEAD metadata."""
    name = row.get("filename", "")
    if not valid_filename(name):
        row["verification_status"] = "INVALID_FILENAME"
        row["error"] = "Unsafe or invalid installer filename in CSV"
        return "INVALID_FILENAME"
    path = directory / name
    if path.is_symlink():
        row["verification_status"] = "INVALID_FILENAME"
        row["error"] = "Installer is a symbolic link; refusing to hash it"
        return "INVALID_FILENAME"
    if not path.is_file():
        row["verification_status"] = "MISSING_LOCAL_FILE"
        row["error"] = "Installer not found in installers directory"
        return "MISSING_LOCAL_FILE"
    etag, remote_size, head_error = "", None, ""
    if not local_only:
        etag, remote_size, head_error = get_cdn_head(row.get("download_url", ""))
    try:
        return inspect_file(row, path, remote_etag=etag,
                            remote_bytes=remote_size, head_error=head_error)
    except OSError:
        row["verification_status"] = "LOCAL_READ_ERROR"
        row["error"] = "Cannot read local installer"
        return "LOCAL_READ_ERROR"


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
        raise RuntimeError(f"Cannot read resume metadata {path.name}") from exc


def save_partial_metadata(path, data):
    temporary = Path(str(path) + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
    os.replace(temporary, path)


def complete_download(row, partial, destination, metadata_path, resumed, *, etag="", total=None):
    """Validate the .part BEFORE promoting it to an .exe; preserve it on mismatch."""
    if total is not None and partial.stat().st_size != total:
        row["verification_status"] = "MISMATCH"
        raise RuntimeError("Partial download size does not match CDN total; .part retained")
    status = inspect_file(row, partial, remote_etag=etag, remote_bytes=total)
    if status == "MISMATCH":
        raise RuntimeError("Downloaded file failed integrity validation; .part retained: " + row["error"])
    os.replace(partial, destination)
    metadata_path.unlink(missing_ok=True)
    row["status"] = "downloaded (resumed)" if resumed else "downloaded"
    print(f"  Integrity: {status} (local SHA-256 recorded)")


def download_one(row, directory, *, local_only=False):
    name = row.get("filename", "")
    if not valid_filename(name):
        raise RuntimeError("Unsafe or invalid installer filename in CSV")
    url = row.get("download_url", "")
    if not https_url(url):
        raise RuntimeError("Invalid or non-HTTPS installer URL")
    destination = directory / name
    if destination.is_symlink():
        raise RuntimeError("Installer is a symbolic link; refusing to overwrite or verify")
    if destination.is_file():
        status = verify_existing(row, directory, local_only=local_only)
        row["status"] = "integrity mismatch" if status == "MISMATCH" else "already exists"
        print(f"  Already saved: {human_size(destination.stat().st_size)}; {status}")
        return
    partial = directory / (name + ".part")
    metadata_path = directory / (name + ".part.json")
    if partial.is_symlink() or metadata_path.is_symlink():
        raise RuntimeError("Partial download or metadata is a symbolic link")
    prior = partial.stat().st_size if partial.is_file() else 0
    old_metadata = get_partial_metadata(metadata_path) if prior else {}
    identity = url_identity(url)
    if prior and old_metadata.get("url_identity") not in (None, identity):
        raise RuntimeError("Existing .part belongs to another URL; move it aside first")
    if prior:
        print(f"  Found unfinished download: {human_size(prior)}; requesting remaining bytes...")
    for attempt in range(2):
        offset = prior if attempt == 0 else 0
        headers = {"User-Agent": "LegacyGamesPersonalBackup/1.2", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
            validator = old_metadata.get("etag")
            if isinstance(validator, str) and validator and not validator.startswith("W/"):
                headers["If-Range"] = validator
            elif old_metadata.get("last_modified"):
                headers["If-Range"] = old_metadata["last_modified"]
        try:
            response = urlopen(Request(url, headers=headers), timeout=60)
        except HTTPError as exc:
            if exc.code == 416 and offset:
                content_range = exc.headers.get("Content-Range", "")
                exc.close()
                matched = re.fullmatch(r"bytes\s+\*/(\d+)", content_range.strip())
                if matched and int(matched.group(1)) == prior:
                    etag, total, head_error = get_cdn_head(url)
                    if total is not None and total != prior:
                        raise RuntimeError("CDN total changed; cannot safely finish .part")
                    old_etag = old_metadata.get("etag", "")
                    if etag and old_etag and etag != old_etag:
                        raise RuntimeError("CDN ETag changed; cannot safely finish .part")
                    complete_download(row, partial, destination, metadata_path, resumed=True,
                                      etag=etag, total=total if total is not None else prior)
                    return
                print("  Saved range cannot be used; restarting installer from zero.")
                continue
            code = exc.code
            exc.close()
            raise RuntimeError(f"Download returned HTTP {code}") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError("Installer download network error (URL omitted)") from exc
        with response:
            status = getattr(response, "status", None) or response.getcode()
            if status not in (200, 206):
                raise RuntimeError(f"Unexpected HTTP {status}")
            if "text/html" in response.headers.get("Content-Type", "").lower():
                raise RuntimeError("Download URL returned HTML, not an installer")
            if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                raise RuntimeError("Encoded installer response cannot be safely verified")
            length = response.headers.get("Content-Length")
            try:
                content_length = int(length) if length is not None else None
                if content_length is not None and content_length < 0:
                    raise ValueError
            except ValueError:
                raise RuntimeError("Invalid CDN Content-Length") from None
            resumable = status == 206 and offset > 0
            start_byte = 0
            expected_total = content_length
            if status == 206:
                match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)",
                                     response.headers.get("Content-Range", "").strip())
                if not match:
                    raise RuntimeError("CDN returned 206 without valid Content-Range")
                first, last = int(match.group(1)), int(match.group(2))
                expected_total = int(match.group(3)) if match.group(3) != "*" else None
                if first != offset or last < first or (expected_total is not None and last >= expected_total):
                    raise RuntimeError("CDN returned unexpected resume byte range")
                if content_length is not None and content_length != last - first + 1:
                    raise RuntimeError("Inconsistent CDN byte range and Content-Length")
                start_byte = first
                if expected_total is None and content_length is not None:
                    expected_total = first + content_length
                if resumable:
                    previous_total = old_metadata.get("total_bytes")
                    if previous_total is not None and expected_total is not None and int(previous_total) != expected_total:
                        raise RuntimeError("Remote installer size changed; cannot safely resume")
                    for key, header in (("etag", "ETag"), ("last_modified", "Last-Modified")):
                        old_value = old_metadata.get(key)
                        new_value = response.headers.get(header)
                        if old_value and new_value and old_value != new_value:
                            raise RuntimeError("Installer changed on CDN; cannot safely resume")
            elif offset:
                print("  CDN ignored byte range; restarting installer from zero.")
            info = {
                "url_identity": identity,
                "etag": response.headers.get("ETag") if not resumable else (response.headers.get("ETag") or old_metadata.get("etag")),
                "last_modified": response.headers.get("Last-Modified") if not resumable else (response.headers.get("Last-Modified") or old_metadata.get("last_modified")),
                "total_bytes": expected_total,
            }
            save_partial_metadata(metadata_path, info)
            meter = ProgressMeter(start_byte, expected_total)
            current = start_byte
            try:
                meter.update(current, force=True)
                with partial.open("ab" if resumable else "wb") as output:
                    while True:
                        chunk = response.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        output.write(chunk)
                        current += len(chunk)
                        meter.update(current)
                meter.update(current, force=True)
            finally:
                meter.close()
            if expected_total is not None and current != expected_total:
                raise RuntimeError(f"Incomplete: {human_size(current)} of {human_size(expected_total)}; .part retained")
            etag = safe_etag(info.get("etag") or "")
            complete_download(row, partial, destination, metadata_path, resumed=resumable,
                              etag=etag, total=expected_total)
            return
    raise RuntimeError("Could not restart after CDN rejected saved byte range")


def run_verification(rows, installers, *, local_only=False, output=None):
    from collections import Counter
    counts = Counter()
    for i, row in enumerate(rows, 1):
        result = verify_existing(row, installers, local_only=local_only)
        counts[result] += 1
        print(f"[{i}/{len(rows)}] {row.get('title') or row.get('filename')}: {result}")
        if output:
            write_exports(rows, output, urls=False)
    print("\nVerification summary:")
    for category in ("VERIFIED_MD5_ETAG", "LOCAL_SHA256_ONLY", "LOCAL_MD5_ONLY",
                     "UNVERIFIED", "MISMATCH", "MISSING_LOCAL_FILE", "INVALID_FILENAME", "LOCAL_READ_ERROR"):
        print(f"  {category}: {counts[category]}")
    print(f"  TOTAL: {len(rows)}")
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export, download, and verify your Legacy Games installers")
    parser.add_argument("--output", default="LegacyGamesBackup", help="Backup folder (default: ./LegacyGamesBackup)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--download", action="store_true", help="Export links and download or verify installers")
    mode.add_argument("--resume", action="store_true", help="Resume from saved CSV without login; verify completed installers")
    mode.add_argument("--verify", action="store_true", help="Verify all local installers against saved hashes and CDN HEAD")
    parser.add_argument("--local-only", action="store_true", help="Skip CDN HEAD requests for existing files (with --verify or --resume)")
    parser.add_argument("--delay", type=float, default=0.2, help="Seconds between API link requests (default: 0.2)")
    args = parser.parse_args(argv)
    if args.delay < 0:
        parser.error("--delay cannot be negative")
    if args.local_only and not (args.verify or args.resume):
        parser.error("--local-only requires --verify or --resume")
    out = Path(args.output).expanduser().resolve()
    try:
        if args.resume or args.verify:
            rows = read_saved_links(out)
            print(f"Loaded {len(rows)} saved games from {out / 'legacygames_links.csv'}")
            if args.resume:
                print("Using saved URLs. If expired, rerun with --download to refresh them.")
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
                return 1
            old = read_saved_links(out) if (out / "legacygames_links.csv").is_file() else []
            rows = merge_prior_verification(resolve_links(games, auth, args.delay), old)
            write_exports(rows, out)
            good = sum(bool(r["download_url"]) for r in rows)
            print(f"Saved {good}/{len(rows)} installer URLs to {out / 'legacygames_urls.txt'}")
        installers = out / "installers"
        if args.verify:
            counts = run_verification(rows, installers, local_only=args.local_only, output=out)
            bad = sum(counts[x] for x in ("MISMATCH", "MISSING_LOCAL_FILE", "INVALID_FILENAME", "LOCAL_READ_ERROR"))
            return 2 if bad else 0
        if args.download or args.resume:
            installers.mkdir(parents=True, exist_ok=True)
            for i, row in enumerate(rows, 1):
                if not row.get("download_url"):
                    continue
                print(f"Downloading [{i}/{len(rows)}]: {row['title']} ({row['filename']})")
                try:
                    download_one(row, installers, local_only=args.local_only)
                except KeyboardInterrupt:
                    row["status"] = "interrupted (resumable)"
                    row["error"] = "Interrupted by user; rerun --resume or --download"
                    write_exports(rows, out)
                    print("\nStopped. Partial files retained; use --resume.", file=sys.stderr)
                    return 130
                except (HTTPError, URLError, OSError, RuntimeError, ValueError, TimeoutError) as exc:
                    row["status"] = "download failed"
                    # All intentional errors avoid printing sensitive download URLs.
                    if not row.get("error"):
                        row["error"] = "Installer download failed; see terminal"
                    print(f"Download failed: {exc}", file=sys.stderr)
                write_exports(rows, out)
            completed = [r for r in rows if r["status"].startswith(("downloaded", "already exists"))]
            total_saved = sum(int(r["bytes"] or 0) for r in completed)
            print(f"Installers saved: {len(completed)}; total size: {human_size(total_saved)}")
        failed = [r for r in rows if r["status"] in ("failed", "download failed", "integrity mismatch")
                  or r.get("verification_status") == "MISMATCH"]
        if failed:
            print(f"WARNING: {len(failed)} failed or mismatched; check CSV status / verification_status.",
                  file=sys.stderr)
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
