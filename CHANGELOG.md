# CHANGELOG — IT Aman v3.4

## v3.4 (2026-04-26) — Security + Bug-Fix Release

### 🔒 Security Fixes (daemon.py)

#### [CRITICAL] Socket permissions: 0o666 → 0o660 + it-aman group
- **Bug:** `os.chmod(SOCKET_PATH, 0o666)` let ANY local user send root-level
  CUPS commands to the daemon (e.g. install drivers, clear jobs, run updates).
- **Fix:** Socket is now owned by `root:it-aman` with mode `0o660`.
  Add GUI users to the group: `usermod -aG it-aman <username>`.
  If the group does not exist, the socket falls back to `0o600` (root-only)
  and a warning is logged.

#### [CRITICAL] SO_PEERCRED peer-credential check in handle_client
- **Bug:** The daemon accepted connections from any process with no identity
  verification, making the 0o666 socket trivially exploitable by malware
  running as an ordinary user.
- **Fix:** `handle_client()` now calls `getsockopt(SO_PEERCRED)` to verify
  that the caller is either `root` (uid 0) or a member of the `it-aman` group
  before dispatching any command.

#### [CRITICAL] validate_command_args whitelist bypass
- **Bug:** The original loop ran over *every* token in the command list.
  Any argument token whose `basename` matched an `ALLOWED_COMMANDS` entry
  (e.g. `/tmp/evil/bash`) was treated as a whitelisted command, allowing
  arbitrary binaries to pass the security gate.
- **Fix:** Only `cmd[0]` is checked against `ALLOWED_COMMANDS`. All
  subsequent tokens are validated solely as safe-character arguments.

#### [CRITICAL] Manifest exception bypassed Ed25519 + SHA256 verification
- **Bug:** In `handle_update_all`, a `try/except` around the manifest
  download silently continued on *any* exception (network error, JSON parse
  failure, signature failure), applying the update without verification.
  A network-level attacker could force a timeout and then serve a tampered
  update from any mirror.
- **Fix:** Any failure to download *or* verify the manifest is now **fatal**.
  The update is rejected with an error response; files are never modified.

### 🐛 Bug Fixes (daemon.py)

#### [BUG] run_command() did not accept env= parameter → TypeError
- The SPRT brand-driver handler called
  `run_command(["bash", installer], timeout=120, env={...})` but
  `run_command`'s signature had no `env` parameter, causing a `TypeError`
  that silently aborted SPRT driver installation.
- **Fix:** Added `env: Optional[Dict[str, str]] = None` to `run_command`.

#### [BUG] SPRT_PPD_DEST path typo 'SPRIT' instead of 'SPRT'
- `SPRT_PPD_DEST = "/usr/share/cups/model/SPRIT/80mmSeries.ppd"` caused
  the PPD to be installed at the wrong path, making SPRT printer setup fail.
- **Fix:** Corrected to `/usr/share/cups/model/SPRT/80mmSeries.ppd`.

#### [BUG] active_threads list could accumulate stale entries
- `client_thread_target` already removes its own thread on exit, but
  unhandled exceptions could leave dead `Thread` objects in the list.
- **Fix:** The accept loop now prunes dead threads every 50 connections
  as a safety net.

---

### 🔒 Security Fixes (printers.sh)

#### [CRITICAL] GitHub PAT hard-coded in source
- `TOKEN="ghp_Kqo0..."` was committed to the repository, exposing a
  GitHub Personal Access Token to anyone who reads the source.
- **Fix:** Token is now read from `$ITAMAN_GITHUB_TOKEN` environment
  variable. If unset, unauthenticated API calls are used (rate-limited
  to 60 req/hour, sufficient for update checks).

#### [HIGH] Self-update written without hash verification or rollback
- The script downloaded and immediately executed a new version with no
  integrity check. A compromised GitHub account or MITM attacker could
  achieve RCE on every machine running the tool.
- **Fix:** SHA256 checksum is downloaded from the repo (`printers.sh.sha256`)
  and verified before the file is installed. Mismatch → update aborted.
  The current version is backed up before replacement; restoration happens
  automatically if `mv` fails.

### 🛠 Improvements (printers.sh — merged from Printers-Tools v1.3)

- **Retry logic:** `check_for_updates` now retries the version fetch up to
  3 times with 2 s / 4 s / 6 s backoff before giving up silently.
- **Variable naming:** `_W` / `_H` renamed to `WIN_W` / `WIN_H` throughout
  for readability and to avoid conflicts with positional parameters.
- **Atomic update:** new script written to a temp file, then `mv`-ed into
  place (atomic on the same filesystem) instead of being piped directly.

---

## v3.3 (2026-04-25)
- Merged Printers-Tools v1.3 thermal brand drivers (xprinter, SPRT).
- Added network_scan handler (subnet TCP scan + mDNS + HTTP model probe).
- Added branch-aware scan filtering.
- Added Ed25519 manifest signing for updates.

## v3.0 (earlier)
- Initial Python daemon + GUI architecture.
- CUPS printer management over Unix socket.
