import os
import signal
import subprocess
import threading
from datetime import datetime, timedelta

import objc
import rumps
from AppKit import NSURL, NSOpenPanel

from sync import (
    APP_AUTHOR,
    APP_BUILD_HASH,
    APP_BUILD_TIME,
    APP_SPONSOR,
    APP_VERSION,
    APP_WEBSITE,
    SyncProgress,
    add_history_entry,
    find_rclone,
    install_launchd_plist,
    install_rclone,
    is_launchd_installed,
    load_config,
    load_history,
    run_sync_live,
    save_config,
    uninstall_app,
    uninstall_launchd_plist,
)


def _pick_folder(title: str, start_path: str | None = None) -> str | None:
    """Open a native macOS folder picker dialog. Returns the chosen path or None if cancelled."""
    panel = NSOpenPanel.openPanel()
    panel.setTitle_(title)
    panel.setCanChooseFiles_(False)
    panel.setCanChooseDirectories_(True)
    panel.setAllowsMultipleSelection_(False)
    panel.setCanCreateDirectories_(False)

    if start_path and os.path.isdir(start_path):
        panel.setDirectoryURL_(NSURL.fileURLWithPath_(start_path))

    if panel.runModal() == objc.YES:
        return str(panel.URL().path())
    return None


class FolderSyncApp(rumps.App):
    def __init__(self):
        super().__init__('FolderSync', quit_button=None)
        self.config = load_config()

        self.status = 'idle'
        self.last_sync = None
        self.last_error = None
        self.sync_thread = None
        self.stop_event = threading.Event()
        self._wake_event = threading.Event()  # wakes the sleep between syncs without stopping
        self._sync_start_time = None
        self._sync_end_time = None
        self._initial_bytes_total = None
        self._next_sync_time = self._load_next_sync_time()
        self._rclone_proc = None  # track rclone subprocess for force-kill on quit
        self.progress = SyncProgress()

        # Flags set by background threads, consumed by UI timer on main thread
        self._ui_dirty = True
        self._rebuild_history = False

        # Menu items
        self.status_item = rumps.MenuItem('Status: Idle')
        self.status_item.set_callback(None)

        self.last_sync_item = rumps.MenuItem('Last sync: Never')
        self.last_sync_item.set_callback(None)

        self.next_sync_item = rumps.MenuItem('Next sync: —')
        self.next_sync_item.set_callback(None)

        # Progress submenu (visible during sync)
        # Use stable keys so items update correctly when titles change
        self.progress_menu = rumps.MenuItem('Progress')
        self.progress_data_item = rumps.MenuItem('Data: —')
        self.progress_data_item.set_callback(None)
        self.progress_speed_item = rumps.MenuItem('Speed: —')
        self.progress_speed_item.set_callback(None)
        self.progress_eta_item = rumps.MenuItem('ETA: —')
        self.progress_eta_item.set_callback(None)
        self.progress_current_item = rumps.MenuItem('File: —')
        self.progress_current_item.set_callback(None)
        self._progress_items = [
            ('data', self.progress_data_item),
            ('speed', self.progress_speed_item),
            ('eta', self.progress_eta_item),
            ('current', self.progress_current_item),
        ]
        for key, item in self._progress_items:
            self.progress_menu[key] = item

        self.toggle_item = rumps.MenuItem('Pause Sync', callback=self.toggle_sync)
        self.sync_now_item = rumps.MenuItem('Sync Now', callback=self.sync_now)

        # Recent syncs submenu
        self.history_menu = rumps.MenuItem('Recent Syncs')
        self._rebuild_history_menu()

        # Configure submenu
        self.configure_menu = rumps.MenuItem('Configure')
        self.source_item = rumps.MenuItem(f'Source: {self.config["source"]}', callback=self.set_source)
        self.dest_item = rumps.MenuItem(f'Destination: {self.config["destination"]}', callback=self.set_destination)
        self.interval_item = rumps.MenuItem(f'Interval: {self.config["interval_minutes"]} min', callback=self.set_interval)
        self.autostart_item = rumps.MenuItem('Start on Login', callback=self.toggle_autostart)
        self.autostart_item.state = is_launchd_installed()
        self.configure_menu[self.source_item.title] = self.source_item
        self.configure_menu[self.dest_item.title] = self.dest_item
        self.configure_menu[self.interval_item.title] = self.interval_item
        self.configure_menu[self.autostart_item.title] = self.autostart_item

        self.open_log_item = rumps.MenuItem('View Log', callback=self.open_log)
        self.about_menu = rumps.MenuItem('About')
        about_version = rumps.MenuItem(f'FolderSync v{APP_VERSION} ({APP_BUILD_HASH})')
        about_version.set_callback(None)
        about_build = rumps.MenuItem(f'Built: {APP_BUILD_TIME}')
        about_build.set_callback(None)
        about_author = rumps.MenuItem(f'By {APP_AUTHOR}')
        about_author.set_callback(None)
        about_website = rumps.MenuItem('Website', callback=lambda _: subprocess.Popen(['open', APP_WEBSITE]))
        about_sponsor = rumps.MenuItem('Sponsor', callback=lambda _: subprocess.Popen(['open', APP_SPONSOR]))
        self.about_menu[about_version.title] = about_version
        self.about_menu[about_build.title] = about_build
        self.about_menu[about_author.title] = about_author
        self.about_menu[about_website.title] = about_website
        self.about_menu[about_sponsor.title] = about_sponsor
        self.uninstall_item = rumps.MenuItem('Uninstall...', callback=self.uninstall)
        self.quit_item = rumps.MenuItem('Quit', callback=self.quit_app)

        self.menu = [
            self.status_item,
            self.last_sync_item,
            self.next_sync_item,
            self.progress_menu,
            None,
            self.sync_now_item,
            self.toggle_item,
            None,
            self.history_menu,
            self.configure_menu,
            self.open_log_item,
            self.about_menu,
            None,
            self.uninstall_item,
            self.quit_item,
        ]

        # UI refresh timer — runs on the main thread, polls state set by background threads.
        # This avoids calling AppKit from background threads (which causes crashes).
        self._ui_timer = rumps.Timer(self._poll_ui, 1)
        self._ui_timer.start()

        self._install_signal_handlers()
        if self.config['enabled']:
            if self._next_sync_time is None:
                # Fresh install or cleared — set to now so first sync runs immediately and config has the value
                self._save_next_sync_time(datetime.now())
            self.start_sync_loop()
        self.update_menu()

    # ── Icon & menu updates ───────────────────────────────────────────

    def update_icon(self):
        icons = {
            'idle': '☁️',
            'syncing': '🔄',
            'error': '⚠️',
            'paused': '⏸️',
        }
        self.title = icons.get(self.status, '☁️')

    def update_menu(self):
        status_labels = {
            'idle': '✅  Status: Idle',
            'syncing': '🔄  Status: Syncing...',
            'error': f'❌  Status: Error — {self.last_error or "unknown"}',
            'paused': '⏸️  Status: Paused',
        }
        self.status_item.title = status_labels.get(self.status, 'Status: Unknown')

        if self.last_sync:
            self.last_sync_item.title = f'Last sync: {self.last_sync}'
        else:
            self.last_sync_item.title = 'Last sync: Never'

        # Next sync time
        if self._next_sync_time and self.config['enabled'] and self.status != 'syncing':
            self.next_sync_item.title = f'Next sync: {self._next_sync_time.strftime("%b %d, %H:%M")}'
        elif self.status == 'syncing':
            self.next_sync_item.title = 'Next sync: now'
        else:
            self.next_sync_item.title = 'Next sync: —'

        # Toggle button state
        has_active_loop = self.sync_thread is not None and self.sync_thread.is_alive()
        is_paused = not self.config['enabled']

        if is_paused:
            self.toggle_item.title = 'Resume Sync'
            self.toggle_item.set_callback(self.toggle_sync)
        elif self.status == 'syncing':
            self.toggle_item.title = 'Pause Sync'
            self.toggle_item.set_callback(self.toggle_sync)
        else:
            # Not syncing — gray out pause
            self.toggle_item.title = 'Pause Sync'
            self.toggle_item.set_callback(None)

        # Sync Now only available when idle with an active loop (not paused, not syncing)
        if is_paused or self.status == 'syncing' or not has_active_loop:
            self.sync_now_item.set_callback(None)
        else:
            self.sync_now_item.set_callback(self.sync_now)

        self.update_icon()

    @staticmethod
    def _parse_bytes(s: str) -> float:
        """Parse a human-readable byte string like '1.234 GiB' to bytes."""
        units = {'B': 1, 'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3, 'TiB': 1024**4}
        parts = s.strip().split()
        if len(parts) == 2 and parts[1] in units:
            try:
                return float(parts[0]) * units[parts[1]]
            except ValueError:
                pass
        return 0.0

    @staticmethod
    def _format_duration(seconds: int) -> str:
        """Format seconds to human-readable duration like '5m30s' or '1h15m'."""
        if seconds < 60:
            return f'{seconds}s'
        minutes, secs = divmod(seconds, 60)
        if minutes < 60:
            return f'{minutes}m{secs}s'
        hours, minutes = divmod(minutes, 60)
        return f'{hours}h{minutes}m'

    @staticmethod
    def _format_speed(bytes_per_sec: float) -> str:
        """Format bytes/sec to human-readable speed."""
        if bytes_per_sec >= 1024**3:
            return f'{bytes_per_sec / 1024**3:.1f} GiB/s'
        if bytes_per_sec >= 1024**2:
            return f'{bytes_per_sec / 1024**2:.1f} MiB/s'
        if bytes_per_sec >= 1024:
            return f'{bytes_per_sec / 1024:.1f} KiB/s'
        return f'{bytes_per_sec:.0f} B/s'

    def _update_progress_menu(self):
        """Update progress menu items from self.progress. Must be called on main thread."""
        progress = self.progress
        # Track the max total seen (total grows as rclone discovers more files during scan)
        if progress.bytes_total:
            if self._initial_bytes_total is None:
                self._initial_bytes_total = progress.bytes_total
            elif self._parse_bytes(progress.bytes_total) > self._parse_bytes(self._initial_bytes_total):
                self._initial_bytes_total = progress.bytes_total
        if progress.checks_total > 0 and progress.percent >= 100:
            # Checksum-only run: transfers done, still checking files
            check_pct = int(progress.checks_done / progress.checks_total * 100) if progress.checks_total else 0
            self.progress_data_item.title = f'Checking: {progress.checks_done} / {progress.checks_total} files ({check_pct}%)'
        elif progress.bytes_transferred and self._initial_bytes_total:
            self.progress_data_item.title = f'Data: {progress.bytes_transferred} / {self._initial_bytes_total} ({progress.percent}%)'
        # Calculate speed and ETA from total transferred / elapsed time
        elapsed = (datetime.now() - self._sync_start_time).total_seconds() if self._sync_start_time else 0
        bytes_per_sec = 0.0
        if elapsed > 0 and progress.bytes_transferred:
            transferred_bytes = self._parse_bytes(progress.bytes_transferred)
            if transferred_bytes > 0:
                bytes_per_sec = transferred_bytes / elapsed
                self.progress_speed_item.title = f'Speed: {self._format_speed(bytes_per_sec)}'
            else:
                self.progress_speed_item.title = 'Speed: —'
        else:
            self.progress_speed_item.title = 'Speed: —'
        # ETA from remaining bytes / speed
        if bytes_per_sec > 0 and self._initial_bytes_total:
            total_bytes = self._parse_bytes(self._initial_bytes_total)
            transferred_bytes = self._parse_bytes(progress.bytes_transferred)
            remaining_bytes = total_bytes - transferred_bytes
            if remaining_bytes > 0:
                eta_seconds = int(remaining_bytes / bytes_per_sec)
                self.progress_eta_item.title = f'ETA: {self._format_duration(eta_seconds)}'
            elif progress.percent >= 100:
                self.progress_eta_item.title = 'ETA: done'
            else:
                # transferred >= tracked total but rclone still scanning/working
                self.progress_eta_item.title = 'ETA: calculating...'
        else:
            self.progress_eta_item.title = 'ETA: —'
        if progress.current_file:
            name = progress.current_file
            max_display = 40
            if len(name) > max_display:
                name = '...' + name[-(max_display - 3) :]
            self.progress_current_item.title = f'File: {name}'

    def _rebuild_history_menu(self):
        # Clear existing items
        for key in list(self.history_menu):
            del self.history_menu[key]

        history = load_history()
        if not history:
            empty_item = rumps.MenuItem('No syncs yet')
            empty_item.set_callback(None)
            self.history_menu[empty_item.title] = empty_item
            return

        for entry in reversed(history[-10:]):
            icon = '✅' if entry.get('success') else '❌'
            ts = entry.get('timestamp', '?')
            detail = entry.get('bytes_transferred', '') if entry.get('success') else (entry.get('error', 'unknown')[:30])
            files = entry.get('files_transferred', 0)
            duration = entry.get('duration_seconds', 0)

            if entry.get('success'):
                label = f'{icon} {ts} — {detail}, {files} files, {duration}s'
            else:
                label = f'{icon} {ts} — {detail}'

            item = rumps.MenuItem(label)
            item.set_callback(None)
            self.history_menu[label] = item

    def _reset_progress_menu(self):
        self.progress_data_item.title = 'Data: —'
        self.progress_speed_item.title = 'Speed: —'
        self.progress_eta_item.title = 'ETA: —'
        self.progress_current_item.title = 'File: —'
        self._initial_bytes_total = None

    def _poll_ui(self, _):
        """Called every second on the main thread by rumps.Timer. Safe to update UI here."""
        # Detect wake from sleep: if next_sync_time is in the past but sync loop
        # is still waiting (Event.wait uses monotonic clock which pauses during sleep),
        # wake it up so it syncs immediately.
        if (
            self._next_sync_time
            and self._next_sync_time < datetime.now()
            and self.status != 'syncing'
            and self.config['enabled']
        ):
            self._wake_event.set()

        if self._rebuild_history:
            self._rebuild_history = False
            self._rebuild_history_menu()

        if self._ui_dirty:
            self._ui_dirty = False
            if self.status == 'syncing':
                self._update_progress_menu()
            self.update_menu()

    def _mark_ui_dirty(self):
        """Signal that UI needs refresh. Safe to call from any thread."""
        self._ui_dirty = True

    # ── Sync loop ─────────────────────────────────────────────────────

    def _ensure_rclone(self) -> bool:
        """Check if rclone is available; if not, offer to install it. Returns True if ready."""
        if find_rclone():
            return True

        response = rumps.alert(
            title='rclone not found',
            message='FolderSync requires rclone to sync files.\n\nInstall it now via Homebrew?',
            ok='Install',
            cancel='Cancel',
        )
        if response != 1:
            rumps.notification('FolderSync', 'rclone required', 'Install rclone manually: brew install rclone', sound=False)
            return False

        rumps.notification('FolderSync', 'Installing rclone...', 'This may take a minute.', sound=False)
        path = install_rclone()
        if path:
            rumps.notification('FolderSync', 'rclone installed', 'Ready to sync.', sound=False)
            return True
        else:
            rumps.notification('FolderSync', 'Installation failed', 'Install manually: brew install rclone', sound=False)
            return False

    def _load_next_sync_time(self) -> datetime | None:
        """Load next_sync_time from config. Returns None if not set or invalid."""
        raw = self.config.get('next_sync_time')
        if raw:
            try:
                return datetime.fromisoformat(raw)
            except (ValueError, TypeError):
                pass
        return None

    def _save_next_sync_time(self, dt: datetime | None):
        """Persist next_sync_time to config file."""
        self._next_sync_time = dt
        self.config['next_sync_time'] = dt.isoformat() if dt else None
        save_config(self.config)

    def start_sync_loop(self):
        if not self._ensure_rclone():
            self.status = 'error'
            self.last_error = 'rclone not installed'
            self.update_menu()
            return
        self.stop_event.clear()
        self._wake_event.clear()
        self.sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
        self.sync_thread.start()

    def _wait_until_next_sync(self, remaining: float) -> bool:
        """Wait for `remaining` seconds, handling wake events (config changes / wake from sleep).
        Returns True if we should sync, False if stopped."""
        self._wake_event.clear()
        while remaining > 0 and not self.stop_event.is_set():
            if self._wake_event.wait(timeout=remaining):
                # Recalculate: next sync = last sync end + new interval
                self._wake_event.clear()
                interval = self.config['interval_minutes'] * 60
                since_last_sync = (datetime.now() - self._sync_end_time).total_seconds() if self._sync_end_time else interval
                remaining = max(0, interval - since_last_sync)
                self._save_next_sync_time(datetime.now() + timedelta(seconds=remaining))
                self._mark_ui_dirty()
            else:
                break  # timeout expired — time to sync
        return not self.stop_event.is_set()

    def _sync_loop(self):
        # Check if we have a saved next_sync_time that hasn't passed yet
        if self._next_sync_time and self._next_sync_time > datetime.now():
            remaining = (self._next_sync_time - datetime.now()).total_seconds()
            self._mark_ui_dirty()
            if not self._wait_until_next_sync(remaining):
                return  # stopped — keep saved next_sync_time for restart
        # Either no saved time, or it has passed — sync now
        self._run_sync()
        while not self.stop_event.is_set():
            interval = self.config['interval_minutes'] * 60
            remaining = interval  # full interval from sync end (we just finished)
            self._save_next_sync_time(datetime.now() + timedelta(seconds=remaining))
            self._mark_ui_dirty()
            if not self._wait_until_next_sync(remaining):
                break  # stopped — keep saved next_sync_time for restart
            if self.config['enabled']:
                self._save_next_sync_time(None)
                self._run_sync()

    def _run_sync(self):
        max_retries = 3
        retry_delay = 30
        non_retryable = {'Google Drive not mounted', 'NAS not mounted', 'rclone not found — run: brew install rclone', 'Sync cancelled'}

        for attempt in range(1, max_retries + 1):
            self._sync_start_time = datetime.now()
            self._initial_bytes_total = None
            self.progress = SyncProgress()
            self.status = 'syncing'
            self._mark_ui_dirty()

            def _progress_callback(progress):
                self.progress = progress
                self._mark_ui_dirty()

            result = run_sync_live(
                self.config['source'],
                self.config['destination'],
                on_progress=_progress_callback,
                stop_event=self.stop_event,
                use_checksum=self.config.get('use_checksum', True),
                on_start=lambda proc: setattr(self, '_rclone_proc', proc),
            )
            self._rclone_proc = None

            if result.success:
                self.status = 'idle'
                self.last_error = None
                self.last_sync = result.timestamp
                break
            elif self.status == 'paused' or self.stop_event.is_set():
                # Cancelled by pause/quit — don't overwrite paused status with error
                break
            elif result.error in non_retryable or attempt == max_retries:
                self.status = 'error'
                self.last_error = result.error
                break
            else:
                # Transient error — retry after delay
                self.status = 'error'
                self.last_error = f'{result.error} (retry {attempt}/{max_retries})'
                self._mark_ui_dirty()
                if self.stop_event.wait(timeout=retry_delay):
                    break  # stopped during retry wait

        self._sync_end_time = datetime.now()
        if result.success or result.error != 'Sync cancelled':
            add_history_entry(result)
        self._rebuild_history = True
        # Wake the sync loop so it recalculates next_sync_time from this sync
        self._wake_event.set()
        self._mark_ui_dirty()

    # ── Signal handling ────────────────────────────────────────────────

    def _install_signal_handlers(self):
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, self._handle_signal)

    def _handle_signal(self, signum, _frame):
        self._shutdown()
        rumps.quit_application()

    # ── Actions ───────────────────────────────────────────────────────

    def toggle_sync(self, _):
        self.config['enabled'] = not self.config['enabled']
        save_config(self.config)

        if self.config['enabled']:
            self.status = 'idle'
            self.start_sync_loop()
        else:
            self.status = 'paused'
            self._save_next_sync_time(None)  # clear scheduled sync when pausing
            self.stop_event.set()

        self.update_menu()

    def sync_now(self, _):
        if self.status == 'syncing':
            return
        # Set next_sync_time to now so the sync loop treats it as overdue,
        # then wake the loop. _poll_ui's wake-from-sleep check also catches this.
        if self.sync_thread and self.sync_thread.is_alive():
            self._save_next_sync_time(datetime.now())
            self._wake_event.set()
        else:
            threading.Thread(target=self._run_sync, daemon=True).start()

    def _save_and_restart(self):
        save_config(self.config)
        self._update_config_menu()
        self._wake_event.set()  # wake sync loop to recalculate next sync time
        rumps.notification('FolderSync', 'Config saved', 'Settings updated.', sound=False)

    def _update_config_menu(self):
        self.source_item.title = f'Source: {self.config["source"]}'
        self.dest_item.title = f'Destination: {self.config["destination"]}'
        self.interval_item.title = f'Interval: {self.config["interval_minutes"]} min'

    def set_source(self, _):
        chosen = _pick_folder('Select Source Folder (Google Drive)', self.config['source'])
        if chosen:
            self.config['source'] = chosen
            self._save_and_restart()

    def set_destination(self, _):
        chosen = _pick_folder('Select Destination Folder (NAS)', self.config['destination'])
        if chosen:
            self.config['destination'] = chosen
            self._save_and_restart()

    def set_interval(self, _):
        w = rumps.Window(
            title='Sync Interval',
            message='Enter sync interval in minutes:',
            default_text=str(self.config['interval_minutes']),
            ok='Save',
            cancel='Cancel',
            dimensions=(420, 24),
        )
        response = w.run()
        if response.clicked and response.text.strip():
            try:
                minutes = int(response.text.strip())
                if minutes < 1:
                    rumps.notification('FolderSync', 'Error', 'Interval must be at least 1 minute.', sound=False)
                    return
                self.config['interval_minutes'] = minutes
                self._save_and_restart()
            except ValueError:
                rumps.notification('FolderSync', 'Error', 'Please enter a valid number.', sound=False)

    def toggle_autostart(self, _):
        if is_launchd_installed():
            uninstall_launchd_plist()
            self.autostart_item.state = False
            rumps.notification('FolderSync', 'Auto-start disabled', 'App will no longer start on login.', sound=False)
        elif install_launchd_plist():
            self.autostart_item.state = True
            rumps.notification('FolderSync', 'Auto-start enabled', 'App will start automatically on login.', sound=False)
        else:
            rumps.notification('FolderSync', 'Error', 'App not found in /Applications. Install first.', sound=False)

    def open_log(self, _):
        log = os.path.expanduser('~/foldersync.log')
        if os.path.exists(log):
            script = (
                f'tell application "Terminal"\n'
                f'  activate\n'
                f'  do script "tail -n 80 -f {log}"\n'
                f'end tell'
            )
            subprocess.Popen(['osascript', '-e', script])
        else:
            rumps.notification('FolderSync', 'No log yet', 'Run a sync first.', sound=False)

    def uninstall(self, _):
        response = rumps.alert(
            title='Uninstall FolderSync',
            message='This will remove the app, config, history, and log files.\nYour Google Drive and NAS folders will NOT be touched.',
            ok='Uninstall',
            cancel='Cancel',
        )
        if response == 1:
            self._shutdown()
            removed = uninstall_app()
            rumps.notification('FolderSync', 'Uninstalled', f'Removed {len(removed)} items. Goodbye!', sound=False)
            rumps.quit_application()

    def _shutdown(self):
        """Stop sync and kill any running rclone subprocess."""
        self.stop_event.set()
        self._wake_event.set()
        if self._ui_timer.is_alive():
            self._ui_timer.stop()
        proc = self._rclone_proc
        if proc:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except OSError:
                pass

    def quit_app(self, _):
        self._shutdown()
        rumps.quit_application()


if __name__ == '__main__':
    FolderSyncApp().run()
