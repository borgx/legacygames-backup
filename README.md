# Legacy Games Library Backup

Back up installers for the games owned by **your Legacy Games account**. This is a community utility, not an official Legacy Games tool.

**Features**

- Export game names and direct installer download URLs to CSV and TXT.
- Download `.exe` installers with a live progress bar, download size / total size, and transfer speed.
- Resume interrupted downloads from `.part` files using HTTP byte-range requests where supported.
- Skip existing completed installers and calculate SHA-256 file hashes.
- Discover purchases and Prime/Luna giveaways associated with your account, if returned by the Legacy Games API.
- Runs on Python 3.9+ using only the standard library.

> **Status — tested with a real account (user-reported, October 2026):** A user successfully signed in, exported **64/64 installer links**, and downloaded **64 installers totaling 31.0 GiB**. The same user also tested **`--resume` with a partially downloaded installer**: it continued from **677.0 MiB** to completion, while other previously completed installers were skipped. These results document one account and environment; compatibility with all accounts or future API changes is not guaranteed. Review the script before entering credentials.

## Real-world test results

In a user-provided terminal log, the script returned `Found 64 owned games`, exported `64/64` installer URLs, and finished with:

```text
Installers saved: 64; total size: 31.0 GiB
```

The user then tested recovery from an existing partial download using `--resume`. The terminal log included:

```text
Loaded 64 saved games from legacygames_links.csv
Found unfinished download: 677.0 MiB; requesting remaining bytes...
[##########################] 100.0%  1.2 GiB / 1.2 GiB
Already saved: 357.6 MiB (skipped)
Installers saved: 64; total size: 31.0 GiB
```

These are user-reported results, not independent testing against every account or every CDN edge case. The log shows SHA-256 calculations were initiated but does not independently verify installer execution or compare hashes against publisher-provided checksums. Identifying account details, local paths, and game-specific download URLs were intentionally omitted.

## Requirements

Install [Python 3.9 or newer](https://www.python.org/downloads/). On Windows, make sure the Python launcher (`py`) is installed.

## Quick start (Windows PowerShell)

Save `legacygames_backup.py`, then open PowerShell in that folder.

**Export installer URLs only:**

```powershell
py legacygames_backup.py
```

**Export URLs and download installers:**

```powershell
py legacygames_backup.py --download --output "D:\LegacyGamesBackup"
```

**Resume previously interrupted downloads without logging in again:**

```powershell
py legacygames_backup.py --resume --output "D:\LegacyGamesBackup"
```

**Refresh links and resume** (useful if the previously exported URLs no longer work):

```powershell
py legacygames_backup.py --download --output "D:\LegacyGamesBackup"
```

Use the **same output folder** when resuming. Without `--output`, the default is `./LegacyGamesBackup`.

On macOS/Linux, use `python3` instead of `py` (the downloads are Windows `.exe` installers).

The program prompts for your Legacy Games email/username and password. The password is not saved to disk by the script. Credentials are sent directly to the Legacy Games API over HTTPS; inspect the source before using the script. The program does not install or execute downloaded `.exe` files.

## Download progress and resuming

Example progress:

```text
Downloading [3/20]: Example Game (ExampleGame.exe)
  Found unfinished download: 125.0 MiB; requesting remaining bytes...
  [#############-------------] 50.0%  250.0 MiB / 500.0 MiB  5.2 MiB/s
```

If a server supports HTTP byte ranges, an incomplete `.exe.part` can resume from the point already downloaded. If it ignores the byte range request, the script restarts that installer's download rather than appending duplicated bytes. Available remote ETag / Last-Modified and total-size data help identify changes. Incomplete downloads are kept after Ctrl+C.

The script displays `unknown total` when the server does not report a download size. Completed installers are skipped; their local SHA-256 is recorded, but they are **not compared against an authenticated remote checksum**.

## Output files

| File | Purpose |
| --- | --- |
| `legacygames_links.csv` | Owned-game titles, direct URLs, status, file sizes, SHA-256 values, errors |
| `legacygames_urls.txt` | Plain-text list of download URLs |
| `installers/*.exe` | Completed installer backups |
| `installers/*.exe.part` | Incomplete installers retained for resume |
| `installers/*.exe.part.json` | HTTP resume metadata |

**Privacy:** Never publish your backup folder, game export CSV/TXT, API credentials, or private download URLs. Those URLs may contain account-specific or time-limited access tokens. The included `.gitignore` excludes common generated files, but review every file before committing it.

## Notes and limitations

- There is no publicly accessible complete index of every installer on the CDN; this tool is for **games associated with your own account**.
- If no games are returned, verify that your purchases/giveaways use the login account. Prime/Luna recipients may need to set/reset a Legacy Games password.
- The script saves Windows game installers only; game saves and other local data need separate backups.
- Download URLs may expire; rerun with `--download` to obtain fresh URLs.
- This is an independent community project; it is not affiliated with or endorsed by Legacy Games.

## Attribution

The Legacy Games API implementation was developed with reference to [Heirloom Games Manager](https://github.com/HeirloomGM/heirloom-gm), an MIT-licensed project by Jason A. Tripp. See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) for the attribution and upstream license notice.

## License

MIT — see [`LICENSE`](LICENSE).
