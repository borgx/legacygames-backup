# Legacy Games Library Backup

Back up Windows installers for games owned by **your Legacy Games account**. This is a community tool, **not** an official Legacy Games utility. Built with reference to [Heirloom Games Manager](https://github.com/HeirloomGM/heirloom-gm); see [third-party notices](THIRD_PARTY_NOTICES.md).

Python 3.9+ · standard library only · Windows, Linux, macOS (downloaded installers are Windows `.exe` files).

## Features

- Export purchased and Prime/Luna giveaway titles and direct installer URLs to CSV/TXT.
- Download installers with progress/speed reporting and resume incomplete `.exe.part` files using HTTP byte ranges.
- **Verify completed installers against the CDN's current ETag (when it is strong and MD5-shaped) and Content-Length.**
- **Recompute local SHA-256 and compare against the hash already recorded in `legacygames_links.csv`.** Do not silently replace a mismatching saved checksum.
- Verify your existing backup library using `--verify`: no new installers downloaded, no login required.
- Retain original CSV schema and add `md5`, `cdn_etag`, `cdn_bytes`, `verification_status`; read old CSVs automatically.
- Never overwrite a completed installer automatically. A failed new-download validation leaves the `.part` file in place.

**Real-world verification (user-reported, October 2026):** One account exported and downloaded 64/64 installers (31.0 GiB). A separate HEAD-based checksum probe later compared all **64** local MD5 digests to the current CDN's 32-hex ETags and independently compared their SHA-256 hashes against the original backup CSV. **64/64 matched**; file sizes also matched. This is evidence for those particular files and CDN responses, **not a guarantee** that every CDN ETag equals an MD5 checksum or proof of authentic publisher SHA-256.

> **Integrity vs authenticity:** A 32-hex strong ETag is treated as an *MD5 candidate*, not a guaranteed content hash; a local match is useful for detecting accidental corruption. MD5 is not collision resistant. Neither this nor a locally stored SHA-256 establishes the publisher's cryptographic authenticity. An ETag may change after a legitimate server-side update.

## Quick start

Save `legacygames_backup.py` and run from its folder. The default backup directory is `./LegacyGamesBackup` relative to your current working directory.

| Purpose | Linux/macOS (`python3`) | Windows PowerShell (`py`) |
| --- | --- | --- |
| Export installer URLs | `python3 legacygames_backup.py` | `py legacygames_backup.py` |
| Download / refresh links | `python3 legacygames_backup.py --download` | `py legacygames_backup.py --download` |
| Resume with saved URLs | `python3 legacygames_backup.py --resume` | `py legacygames_backup.py --resume` |
| **Verify all completed backups** | `python3 legacygames_backup.py --verify` | `py legacygames_backup.py --verify` |
| **Verify against saved hashes only (offline)** | `python3 legacygames_backup.py --verify --local-only` | `py legacygames_backup.py --verify --local-only` |

To target an existing backup outside the current directory:

```bash
python3 legacygames_backup.py --verify --output /path/to/LegacyGamesBackup
```

Or, for the original Windows example:

```powershell
py legacygames_backup.py --download --output "D:\LegacyGamesBackup"
py legacygames_backup.py --verify --output "D:\LegacyGamesBackup"
```

Use the **same `--output` folder** for future resume and verification operations.

### Recommended upgrade path for pre-verification backups

1. Keep a separate copy of `legacygames_links.csv` before your first upgrade (especially if syncing your backup folder).
2. Replace your existing `legacygames_backup.py` with this version.
3. Run `python3 legacygames_backup.py --verify --output /path/to/LegacyGamesBackup`. **This reads your existing local installers and makes HEAD requests; it does not download EXEs or prompt for your account password.**
4. Review the terminal summary and the new `verification_status`, `md5`, `cdn_etag`, `cdn_bytes` fields in the CSV. A one-time `legacygames_links.csv.bak` is automatically preserved on the first CSV update.
5. For interrupted downloads, run `--resume`. If saved URLs have expired, run `--download` to authenticate and obtain fresh URLs. On refresh, the program carries over old hashes for the same game/installer filename rather than erasing them.

**Do not run `--download` just to verify an existing library.** `--verify` is the read-only-for-installers operation you want.

## How verification works

When verifying an existing installer:

1. Read its filename and original SHA-256 from `legacygames_links.csv`.
2. Read local file bytes once to compute **MD5 and SHA-256**.
3. Unless `--local-only` is set, perform an **HTTP HEAD** against the saved HTTPS download URL, looking for `ETag` and `Content-Length`. No installer body is downloaded.
4. Check SHA-256 against the **previously saved** SHA-256; MD5 against any previously saved MD5; current and recorded lengths against local bytes; and MD5 against a current strong, plain **32-hex ETag**, if provided.
5. If anything differs, set `verification_status=MISMATCH`, provide a reason in `error`, and **do not replace previously saved hashes**. A mismatch could mean local corruption **or** a legitimate CDN installer update; examine it before changing anything.
6. If all available checks pass, fill in the new metadata and record a verification result.

For a *new* download, the program checks the final size and computes hashes **on the `.part` file before renaming it to `.exe`**. It compares MD5 against a strong MD5-shaped ETag from the actual GET response, when available. If validation fails, the `.part` is retained rather than being promoted.

### Verification statuses

| `verification_status` | Meaning |
| --- | --- |
| `VERIFIED_MD5_ETAG` | Local file's MD5 matches the current CDN ETag candidate; available SHA-256 and size checks also passed. |
| `LOCAL_SHA256_ONLY` | Local SHA-256 matches the previously saved SHA-256, but there was no usable current remote MD5 ETag. |
| `LOCAL_MD5_ONLY` | Historical local MD5 agrees, no saved SHA-256 and no usable ETag. |
| `UNVERIFIED` | No prior checksum and no usable current remote ETag; a new baseline may be recorded, but no independent match was available. |
| `MISMATCH` | One or more of saved SHA-256, saved MD5, byte counts, or current ETag disagree. Saved hashes remain unchanged. |
| `MISSING_LOCAL_FILE` | No completed `.exe` found in `installers/`. |
| `INVALID_FILENAME` | The CSV's filename is unsafe/invalid, or the installer is a symlink. |
| `LOCAL_READ_ERROR` | Installer could not be read locally. |

During a full `--verify`, **exit code 0** means no mismatches/missing/unreadable files were detected. `LOCAL_SHA256_ONLY` and `UNVERIFIED` do **not** imply remote verification; inspect the counts. **Exit code 2** means there was a mismatch, missing file, unsafe filename, or local read error. Other fatal errors return `1`; Ctrl+C returns `130`.

### CDN limitations

- The Legacy Games `/products/download` API response observed during testing contained `id`, `name`, and `file`, **not an authoritative SHA-256**.
- For the 64 tested objects, CDN `ETag` values matched full-file MD5 values exactly. Some CDNs use multipart, non-content, or weak ETags; those are **not** interpreted as MD5 here.
- HTTP `ETag` is HTTP response metadata, not a signed publisher manifest. `Content-Length` is similarly not a cryptographic checksum.
- Expired links, network failures, missing ETags, and `403`/`405` HEAD responses can prevent CDN verification; when available, **prior local SHA-256** can still be checked.
- On a remote update, a locally intact older backup may report `MISMATCH` against the newer ETag. The tool does **not** silently delete, replace, or redownload existing completed files.
- For pre-existing old CSVs, a saved SHA-256 is checked; it is never overwritten on disagreement. Files lacking any historical hash cannot be retrospectively authenticated by computing a new local SHA-256 alone.

## Downloading and resuming

Example:

```text
Downloading [3/20]: Example Game (ExampleGame.exe)
  Found unfinished download: 125.0 MiB; requesting remaining bytes...
  [#############-------------] 50.0%  250.0 MiB / 500.0 MiB  5.2 MiB/s
  Integrity: VERIFIED_MD5_ETAG (local SHA-256 recorded)
```

When supported, partial downloads use `Range` and `If-Range` with the stored ETag/Last-Modified. If the CDN ignores Range, the `.part` is safely restarted rather than appended incorrectly. An interrupted `.part` file remains available for the next `--resume`. If a response's ETag or file size changes mid-resume, the script stops instead of combining versions.

Completed installers are **never overwritten** by `--download` or `--resume`; they are verified/skipped instead. This can require re-reading the existing files and issuing HEAD requests. In a failure, inspect the CSV `error` column.

## Output files

```text
LegacyGamesBackup/
├── legacygames_links.csv       # Includes URLs, saved SHA-256, MD5, ETag, verification result
├── legacygames_links.csv.bak   # One-time backup of the previous CSV on first update
├── legacygames_urls.txt        # Direct installer URLs
└── installers/
    ├── Game.exe                # Completed installer
    ├── Game.exe.part           # Incomplete or rejected download
    └── Game.exe.part.json      # Resume validators
```

### CSV columns

The original columns are unchanged and in the same order:

`title, source, game_id, product_id, installer_uuid, filename, download_url, reported_size, status, bytes, sha256, error`

The new columns are appended:

| Column | Meaning |
| --- | --- |
| `md5` | Locally computed full-file MD5. Not a publisher-signed digest. |
| `cdn_etag` | Most recently available ETag response header; may not be a checksum. |
| `cdn_bytes` | Most recently available CDN-reported total size in bytes. |
| `verification_status` | Latest verification status from the table above. |

`--verify` updates the CSV with the new fields and current verification outcome; it does **not** modify, download, or execute installer EXEs. It does not rewrite `legacygames_urls.txt`. When refreshing links with `--download`, hashes from existing matching records are preserved.

## Privacy and safety

- The script prompts for credentials only when exporting/refetching links. Passwords are not written to disk; they are sent over HTTPS to the Legacy Games API. Inspect the code before entering credentials.
- **Do not publish** your backup folder, CSV, TXT, `.bak`, installer archives, or authenticated URLs. URLs may contain private/account-specific or expiring information. `.gitignore` excludes typical generated backup data, but always check `git status` before committing.
- Installers are saved; the script does **not** execute or install them.
- There is no public complete CDN installer catalog; the utility discovers games associated with your account.
- Game saves and installed game data are outside the scope of this tool.

## Tests

Run the offline standard-library regression tests:

```bash
python3 -m unittest discover -s tests -v
```

No test downloads a real installer. Tests simulate CDN response headers and local files. Live Legacy Games API/CDN integration needs testing from a real account.

## Attribution

The Legacy Games API implementation is based on [Heirloom Games Manager](https://github.com/HeirloomGM/heirloom-gm) (MIT; Jason A. Tripp). See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

## License

MIT — see [`LICENSE`](LICENSE).
