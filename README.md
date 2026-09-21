# Encrypted scoreboard raw data

This branch stores durable raw market data and reviewed evidence for the scoreboard.
Objects are compressed and AES-256-GCM encrypted; the decryption key is stored in
GitHub Actions secret RAW_STATE_KEY. No credentials or browser cookies are included.

Restore using cloud_state.py from the main branch. See docs/CLOUD_OPERATION.md.
Initial migration: 2026-09-21. Initial verified price cutoff: 2026-09-18.
This is not a website branch. Previous Git generations remain available.