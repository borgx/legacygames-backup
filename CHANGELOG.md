# Changelog

## v0.2.0 — CDN integrity verification

- Introduce `--verify` to check all existing installers without downloading EXEs or authenticating again, using saved CSV URLs and HTTP HEAD requests.
- Introduce `--local-only` (with `--verify` / `--resume`) for offline verification against previously recorded local checksums.
- Append `md5`, `cdn_etag`, `cdn_bytes`, `verification_status` CSV columns; automatically migrate old backup CSVs.
- Compare local full-file MD5 to strong, 32-hex CDN ETags when present, and file size against CDN Content-Length.
- Compare current SHA-256 against the previous saved SHA-256; never silently overwrite a mismatch.
- Validate new/resumed `.part` installers **before** promoting to completed `.exe`; preserve partial files on checksum failure.
- Preserve existing manifest checksums when refreshed API links point to the same game and filename.
- Add one-time `.bak` backup and atomic CSV writes, HEAD redirect safeguards and offline regression tests.
- Document MD5/ETag limitations, changes in CDN content, privacy and upgrade instructions.
