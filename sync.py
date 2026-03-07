import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

APP_VERSION = '1.0.0'
APP_BUILD_TIME = 'dev'  # stamped by build.sh
APP_BUILD_HASH = 'dev'  # stamped by build.sh
APP_AUTHOR = 'Ran Isenberg'
APP_WEBSITE = 'https://www.ranthebuilder.cloud'
APP_SPONSOR = 'https://github.com/sponsors/ran-isenberg'

CONFIG_FILE = os.path.expanduser('~/.foldersync.json')
HISTORY_FILE = os.path.expanduser('~/.foldersync-history.json')
LOG_FILE = os.path.expanduser('~/foldersync.log')
APP_PATH = '/Applications/FolderSync.app'
MAX_HISTORY = 20
MAX_LOG_BYTES = 512 * 1024  # 512 KB — keep log file from growing unbounded

LAUNCHD_LABEL = 'com.ranthebuilder.foldersync'
LAUNCHD_PLIST = os.path.expanduser(f'~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist')

DEFAULT_CONFIG = {
    'source': '/Volumes/Google Drive/My Drive',
    'destination': '/Volumes/NAS',
    'interval_minutes': 5,
    'enabled': True,
    'use_checksum': True,
}

# rclone --stats-one-line output patterns
# With --progress: "Transferred:   1.234 GiB / 5.678 GiB, 22%, 10.5 MiB/s, ETA 5m30s"
# Without (piped): "    1.234 GiB / 5.678 GiB, 22%, 10.5 MiB/s, ETA 5m30s"
_RE_TRANSFER_STATS = re.compile(
    r'(?:Transferred:\s+)?'
    r'(?P<transferred>[\d.]+ \S+)\s*/\s*(?P<total>[\d.]+ \S+),\s*'
    r'(?P<percent>\d+|-)%?'
    r'(?:,\s*(?P<speed>[\d.]+ \S+/s))?'
    r'(?:,\s*ETA\s*(?P<eta>\S+))?'
)

# "Transferred:   10 / 50, 20%" or "10 / 50, 20%"
_RE_FILE_STATS = re.compile(r'(?:Transferred:\s+)?(?P<done>\d+)\s*/\s*(?P<total>\d+),\s*(?P<percent>\d+)%')

# "Transferring:\n *  filename.ext: 22% /1.2Mi, 500Ki/s, 1s"
_RE_CURRENT_FILE = re.compile(r'^\s*\*\s+(?P<name>.+?):\s*(?P<detail>.+)$')

# rclone -v INFO line: "2026/03/06 17:45:33 INFO  : file.txt: Copied (server-side copy)"
_RE_INFO_FILE = re.compile(r'INFO\s*:\s*(?P<name>.+?):\s*(?P<detail>Copied|Moved|Deleted|Updated|Unchanged|Skipped)')

# Checksum checking progress: "(chk#1179/7657)" on transfer line
_RE_CHECK_STATS = re.compile(r'\(chk#(?P<done>\d+)/(?P<total>\d+)\)')

# "Checks:  1179 / 7657, 15%" — separate checks line from rclone
_RE_CHECKS_LINE = re.compile(r'Checks:\s+(?P<done>\d+)\s*/\s*(?P<total>\d+),\s*(?P<percent>\d+)%')

# Log prefix pattern: "2026/03/06 17:45:33 NOTICE: " or "2026/03/06 17:45:33 INFO  : "
_RE_LOG_PREFIX = re.compile(r'^\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}\s+\w+\s*:\s*')


@dataclass
class SyncProgress:
    bytes_transferred: str = ''
    bytes_total: str = ''
    percent: int = 0
    speed: str = ''
    eta: str = ''
    files_done: int = 0
    files_total: int = 0
    checks_done: int = 0
    checks_total: int = 0
    current_file: str = ''
    current_file_detail: str = ''


@dataclass
class SyncResult:
    timestamp: str = ''
    success: bool = False
    error: str | None = None
    bytes_transferred: str = ''
    files_transferred: int = 0
    duration_seconds: int = 0


def parse_stats_line(line: str, progress: SyncProgress) -> SyncProgress:
    """Parse a single line of rclone --stats output and update progress in-place."""
    # Strip rclone log prefix (timestamp + level) if present
    stripped = _RE_LOG_PREFIX.sub('', line)

    # Try data transfer stats first (more specific pattern)
    m = _RE_TRANSFER_STATS.search(stripped)
    if m:
        progress.bytes_transferred = m.group('transferred')
        progress.bytes_total = m.group('total')
        pct = m.group('percent')
        progress.percent = int(pct) if pct != '-' else 0
        if m.group('speed'):
            progress.speed = m.group('speed')
        if m.group('eta'):
            progress.eta = m.group('eta')
        # Check for checksum progress on the same line: (chk#1179/7657)
        chk = _RE_CHECK_STATS.search(stripped)
        if chk:
            progress.checks_done = int(chk.group('done'))
            progress.checks_total = int(chk.group('total'))
        return progress

    # Try (chk#N/M) anywhere on the line (fallback if transfer stats regex didn't match)
    chk = _RE_CHECK_STATS.search(stripped)
    if chk:
        progress.checks_done = int(chk.group('done'))
        progress.checks_total = int(chk.group('total'))
        return progress

    # Try checks line: "Checks:  1179 / 7657, 15%"
    m = _RE_CHECKS_LINE.search(stripped)
    if m:
        progress.checks_done = int(m.group('done'))
        progress.checks_total = int(m.group('total'))
        return progress

    # Try file count stats
    m = _RE_FILE_STATS.search(stripped)
    if m:
        progress.files_done = int(m.group('done'))
        progress.files_total = int(m.group('total'))
        return progress

    # Try current file (from --progress output)
    m = _RE_CURRENT_FILE.search(stripped)
    if m:
        progress.current_file = m.group('name')
        progress.current_file_detail = m.group('detail')
        return progress

    # Try INFO file activity (from -v output)
    m = _RE_INFO_FILE.search(line)
    if m:
        progress.current_file = m.group('name')
        progress.current_file_detail = m.group('detail')
        return progress

    return progress


def load_config(config_file: str = CONFIG_FILE) -> dict:
    if os.path.exists(config_file):
        try:
            with open(config_file) as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except (json.JSONDecodeError, ValueError):
            pass  # Corrupt file — fall through to recreate with defaults
    # First launch or corrupt file — create the config file with defaults
    config = DEFAULT_CONFIG.copy()
    save_config(config, config_file)
    return config


def save_config(config: dict, config_file: str = CONFIG_FILE) -> None:
    # Atomic write: write to temp file then rename, so a crash mid-write
    # never leaves a corrupt/empty config file.
    tmp_file = config_file + '.tmp'
    with open(tmp_file, 'w') as f:
        json.dump(config, f, indent=2)
    os.replace(tmp_file, config_file)


def install_launchd_plist() -> bool:
    """Install a launchd plist so the app starts automatically on login. Returns True on success."""
    if not os.path.isdir(APP_PATH):
        return False

    plist = {
        'Label': LAUNCHD_LABEL,
        'ProgramArguments': [os.path.join(APP_PATH, 'Contents', 'MacOS', 'FolderSync')],
        'RunAtLoad': True,
        'KeepAlive': False,
    }

    os.makedirs(os.path.dirname(LAUNCHD_PLIST), exist_ok=True)
    with open(LAUNCHD_PLIST, 'wb') as f:
        plistlib.dump(plist, f)

    subprocess.run(['launchctl', 'unload', LAUNCHD_PLIST], capture_output=True, check=False)
    subprocess.run(['launchctl', 'load', LAUNCHD_PLIST], capture_output=True, check=False)
    return True


def uninstall_launchd_plist() -> bool:
    """Remove the launchd plist so the app no longer starts on login. Returns True if removed."""
    if not os.path.exists(LAUNCHD_PLIST):
        return False
    subprocess.run(['launchctl', 'unload', LAUNCHD_PLIST], capture_output=True, check=False)
    os.remove(LAUNCHD_PLIST)
    return True


def is_launchd_installed() -> bool:
    """Check if the launchd plist is currently installed."""
    return os.path.exists(LAUNCHD_PLIST)


def cleanup_app_data() -> list[str]:
    """Remove all app data files (config, history, log). Returns list of removed paths.
    Does NOT touch Google Drive or NAS folders.
    """
    removed = []
    for path in [CONFIG_FILE, HISTORY_FILE, LOG_FILE]:
        if os.path.exists(path):
            os.remove(path)
            removed.append(path)
    return removed


def uninstall_app() -> list[str]:
    """Remove the app from /Applications and clean up all app data.
    Does NOT touch Google Drive or NAS folders.
    """
    removed = cleanup_app_data()
    if uninstall_launchd_plist():
        removed.append(LAUNCHD_PLIST)
    if os.path.isdir(APP_PATH):
        shutil.rmtree(APP_PATH)
        removed.append(APP_PATH)
    return removed


def load_history(history_file: str = HISTORY_FILE) -> list[dict]:
    if os.path.exists(history_file):
        try:
            with open(history_file) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            pass  # Corrupt file — return empty
    return []


def save_history(history: list[dict], history_file: str = HISTORY_FILE) -> None:
    tmp_file = history_file + '.tmp'
    with open(tmp_file, 'w') as f:
        json.dump(history[-MAX_HISTORY:], f, indent=2)
    os.replace(tmp_file, history_file)


def add_history_entry(result: SyncResult, history_file: str = HISTORY_FILE) -> None:
    history = load_history(history_file)
    history.append(
        {
            'timestamp': result.timestamp,
            'success': result.success,
            'error': result.error,
            'bytes_transferred': result.bytes_transferred,
            'files_transferred': result.files_transferred,
            'duration_seconds': result.duration_seconds,
        }
    )
    save_history(history, history_file)


def truncate_log(log_file: str = LOG_FILE, max_bytes: int = MAX_LOG_BYTES) -> None:
    """Truncate the log file to approximately max_bytes, keeping the tail."""
    if not os.path.exists(log_file):
        return
    size = os.path.getsize(log_file)
    if size <= max_bytes:
        return
    with open(log_file, 'rb') as f:
        f.seek(size - max_bytes)
        f.readline()  # skip partial first line
        tail = f.read()
    with open(log_file, 'wb') as f:
        f.write(tail)


def validate_paths(source: str, destination: str) -> str | None:
    """Return an error message if paths are invalid, None if OK."""
    if not os.path.isdir(source):
        return 'Google Drive not mounted'
    if not os.path.isdir(destination):
        return 'NAS not mounted'
    return None


def find_rclone() -> str | None:
    """Find the rclone binary, checking the app bundle first, then common paths."""
    # Check inside the .app bundle (Contents/Resources/rclone next to Contents/MacOS/)
    if getattr(sys, 'frozen', False):
        bundle_rclone = os.path.join(os.path.dirname(sys.executable), '..', 'Resources', 'rclone')
        bundle_rclone = os.path.normpath(bundle_rclone)
        if os.path.isfile(bundle_rclone) and os.access(bundle_rclone, os.X_OK):
            return bundle_rclone

    path = shutil.which('rclone')
    if path:
        return path
    for candidate in ['/opt/homebrew/bin/rclone', '/usr/local/bin/rclone', '/usr/bin/rclone']:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def install_rclone() -> str | None:
    """Attempt to install rclone via Homebrew. Returns the path if successful, None otherwise."""
    # Try Homebrew first
    brew_paths = ['/opt/homebrew/bin/brew', '/usr/local/bin/brew']
    brew = None
    for candidate in brew_paths:
        if os.path.isfile(candidate):
            brew = candidate
            break
    if not brew:
        brew = shutil.which('brew')

    if brew:
        try:
            result = subprocess.run([brew, 'install', 'rclone'], capture_output=True, text=True, timeout=300, check=False)
            if result.returncode == 0:
                return find_rclone()
        except (subprocess.TimeoutExpired, OSError):
            pass

    return None


def build_rclone_command(source: str, destination: str, log_file: str | None = None, use_checksum: bool = False, live: bool = False) -> list[str]:
    if log_file is None:
        log_file = os.path.expanduser('~/foldersync.log')
    rclone_bin = find_rclone()
    if not rclone_bin:
        raise FileNotFoundError('rclone not found — run: brew install rclone')
    cmd = [
        rclone_bin,
        'sync',
        source,
        destination,
        '--transfers=4',
        '--checkers=8',
        '--stats=0.5s',
        '--stats-one-line',
    ]
    if live:
        # In live mode, output goes to stderr so we can parse progress.
        # We write to the log file ourselves.
        cmd += ['-v']
    else:
        # In non-live mode, rclone writes directly to the log file.
        cmd += [f'--log-file={log_file}', '--log-level=INFO', '--stats-log-level=NOTICE']
    if use_checksum:
        cmd.append('--checksum')
    return cmd


def run_sync(source: str, destination: str, log_file: str | None = None, use_checksum: bool = False) -> tuple[bool, str | None, str | None]:
    """Run rclone sync (blocking, no progress). Returns (success, error_message, timestamp)."""
    path_error = validate_paths(source, destination)
    if path_error:
        return False, path_error, None

    cmd = build_rclone_command(source, destination, log_file, use_checksum=use_checksum)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, check=False)
        if result.returncode == 0:
            timestamp = datetime.now().strftime('%b %d, %H:%M')
            return True, None, timestamp
        else:
            error = result.stderr.strip().split('\n')[-1][:80] if result.stderr else 'rclone error'
            return False, error, None
    except FileNotFoundError:
        return False, 'rclone not found — run: brew install rclone', None
    except subprocess.TimeoutExpired:
        return False, 'Sync timed out', None
    except Exception as e:
        return False, str(e)[:80], None


def run_sync_live(
    source: str,
    destination: str,
    on_progress: Callable[[SyncProgress], None] | None = None,
    stop_event=None,
    log_file: str | None = None,
    use_checksum: bool = False,
    on_start: Callable[[subprocess.Popen], None] | None = None,
) -> SyncResult:
    """Run rclone sync with real-time progress updates via callback."""
    path_error = validate_paths(source, destination)
    if path_error:
        return SyncResult(
            timestamp=datetime.now().strftime('%b %d, %H:%M'),
            success=False,
            error=path_error,
        )

    if log_file is None:
        log_file = os.path.expanduser('~/foldersync.log')
    truncate_log(log_file)
    cmd = build_rclone_command(source, destination, log_file, use_checksum=use_checksum, live=True)
    progress = SyncProgress()
    start_time = datetime.now()
    cancelled = False

    def _kill_on_stop(proc, event):
        """Watch stop_event and terminate rclone immediately when set."""
        event.wait()
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        except OSError:
            pass

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if on_start:
            on_start(proc)

        # Start a watcher thread that kills rclone when stop_event fires
        if stop_event:
            watcher = threading.Thread(target=_kill_on_stop, args=(proc, stop_event), daemon=True)
            watcher.start()

        # Read stderr for progress (rclone -v writes stats + info to stderr)
        stderr_lines: list[str] = []
        with open(log_file, 'a') as log_fh:
            for line in proc.stderr:
                log_fh.write(line)
                log_fh.flush()
                stderr_lines.append(line)
                parse_stats_line(line.strip(), progress)
                if on_progress:
                    on_progress(progress)

        proc.wait()

        cancelled = stop_event and stop_event.is_set()
        duration = int((datetime.now() - start_time).total_seconds())

        if cancelled:
            return SyncResult(
                timestamp=datetime.now().strftime('%b %d, %H:%M'),
                success=False,
                error='Sync cancelled',
                duration_seconds=duration,
            )

        if proc.returncode == 0:
            return SyncResult(
                timestamp=datetime.now().strftime('%b %d, %H:%M'),
                success=True,
                bytes_transferred=progress.bytes_transferred,
                files_transferred=progress.files_done,
                duration_seconds=duration,
            )
        else:
            all_stderr = ''.join(stderr_lines)
            last_line = all_stderr.strip().split('\n')[-1][:80] if all_stderr.strip() else 'rclone error'
            return SyncResult(
                timestamp=datetime.now().strftime('%b %d, %H:%M'),
                success=False,
                error=last_line,
                bytes_transferred=progress.bytes_transferred,
                files_transferred=progress.files_done,
                duration_seconds=duration,
            )
    except FileNotFoundError:
        return SyncResult(
            timestamp=datetime.now().strftime('%b %d, %H:%M'),
            success=False,
            error='rclone not found — run: brew install rclone',
        )
    except Exception as e:
        return SyncResult(
            timestamp=datetime.now().strftime('%b %d, %H:%M'),
            success=False,
            error=str(e)[:80],
            duration_seconds=int((datetime.now() - start_time).total_seconds()),
        )
