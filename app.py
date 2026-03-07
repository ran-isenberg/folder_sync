import os
import signal
import subprocess
import threading
from datetime import datetime, timedelta

import objc
import rumps
from AppKit import (
    NSApp,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSClosableWindowMask,
    NSFont,
    NSMakeRect,
    NSOpenPanel,
    NSTextField,
    NSTitledWindowMask,
    NSURL,
    NSWindow,
)

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


def _create_config_delegate_class():
    """Create an ObjC class to handle button actions in the config window."""

    class ConfigDelegate(objc.lookUpClass('NSObject')):
        config_window = objc.ivar('config_window')

        @objc.python_method
        def initWithConfigWindow_(self, cw):
            self = objc.super(ConfigDelegate, self).init()
            if self is None:
                return None
            self._cw = cw
            return self

        def browseSource_(self, sender):
            self._cw._browse_source()

        def browseDest_(self, sender):
            self._cw._browse_dest()

        def save_(self, sender):
            self._cw.save()

        def cancel_(self, sender):
            self._cw.cancel()

    return ConfigDelegate


_ConfigDelegate = _create_config_delegate_class()


class _ConfigWindow:
    """Native macOS preferences window for FolderSync configuration."""

    WIDTH = 500
    HEIGHT = 280

    def __init__(self, app):
        self.app = app
        self._delegate = _ConfigDelegate.alloc().initWithConfigWindow_(self)
        self._build_window()
        self.window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def _build_window(self):
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, self.WIDTH, self.HEIGHT),
            NSTitledWindowMask | NSClosableWindowMask,
            NSBackingStoreBuffered,
            False,
        )
        self.window.setTitle_('FolderSync — Configure')
        self.window.center()

        content = self.window.contentView()
        y = self.HEIGHT - 50
        label_font = NSFont.boldSystemFontOfSize_(13)

        # Source
        self._add_label(content, 'Source Folder:', 20, y, label_font)
        self.source_field = self._add_text_field(content, self.app.config['source'], 140, y, 240)
        self._add_button(content, 'Browse...', 390, y - 2, 'browseSource:')
        y -= 40

        # Destination
        self._add_label(content, 'Destination:', 20, y, label_font)
        self.dest_field = self._add_text_field(content, self.app.config['destination'], 140, y, 240)
        self._add_button(content, 'Browse...', 390, y - 2, 'browseDest:')
        y -= 40

        # Interval
        self._add_label(content, 'Sync Interval:', 20, y, label_font)
        self.interval_field = self._add_text_field(content, str(self.app.config['interval_minutes']), 140, y, 60)
        self._add_label(content, 'minutes', 208, y, NSFont.systemFontOfSize_(13))
        y -= 40

        # Autostart
        self._add_label(content, 'Start on Login:', 20, y, label_font)
        self.autostart_switch = NSButton.alloc().initWithFrame_(NSMakeRect(140, y - 2, 100, 24))
        self.autostart_switch.setButtonType_(3)  # NSSwitchButton
        self.autostart_switch.setTitle_('')
        self.autostart_switch.setState_(1 if is_launchd_installed() else 0)
        content.addSubview_(self.autostart_switch)
        y -= 50

        # Save / Cancel buttons
        self._add_button(content, 'Cancel', self.WIDTH - 200, y, 'cancel:')
        save_btn = self._add_button(content, 'Save', self.WIDTH - 100, y, 'save:')
        save_btn.setKeyEquivalent_('\r')

    def _add_label(self, parent, text, x, y, font):
        label = NSTextField.labelWithString_(text)
        label.setFrame_(NSMakeRect(x, y, 120, 20))
        label.setFont_(font)
        parent.addSubview_(label)
        return label

    def _add_text_field(self, parent, value, x, y, width):
        field = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y - 2, width, 24))
        field.setStringValue_(value)
        field.setFont_(NSFont.systemFontOfSize_(13))
        parent.addSubview_(field)
        return field

    def _add_button(self, parent, title, x, y, action_sel):
        btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, 90, 30))
        btn.setTitle_(title)
        btn.setBezelStyle_(NSBezelStyleRounded)
        btn.setTarget_(self._delegate)
        btn.setAction_(action_sel)
        parent.addSubview_(btn)
        return btn

    def _browse_source(self):
        chosen = _pick_folder('Select Source Folder (Google Drive)', str(self.source_field.stringValue()))
        if chosen:
            self.source_field.setStringValue_(chosen)

    def _browse_dest(self):
        chosen = _pick_folder('Select Destination Folder (NAS)', str(self.dest_field.stringValue()))
        if chosen:
            self.dest_field.setStringValue_(chosen)

    def cancel(self):
        self.window.close()
        self.app._config_window = None

    def save(self):
        source = str(self.source_field.stringValue()).strip()
        destination = str(self.dest_field.stringValue()).strip()
        try:
            interval = int(str(self.interval_field.stringValue()).strip())
            if interval < 1:
                rumps.notification('FolderSync', 'Error', 'Interval must be at least 1 minute.', sound=False)
                return
        except ValueError:
            rumps.notification('FolderSync', 'Error', 'Please enter a valid number for interval.', sound=False)
            return
        autostart = bool(self.autostart_switch.state())
        self.window.close()
        self.app._config_window = None
        self.app._apply_config(source, destination, interval, autostart)


class FolderSyncApp(rumps.App):
    def __init__(self):
        super().__init__('FolderSync', quit_button=None)
        self.config = load_config()

        self.status = 'idle'
        history = load_history()
        self.last_sync = history[-1]['timestamp'] if history else None
        self.last_error = None
        self.sync_thread = None
        self.stop_event = threading.Event()
        self._wake_event = threading.Event()  # wakes the sleep between syncs without stopping
        self._sync_start_time = None
        self._sync_end_time = self._load_sync_end_time()
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

        # Single progress line (grayed out, shown only during sync)
        self.progress_item = rumps.MenuItem('')
        self.progress_item.set_callback(None)
        self.progress_item.hidden = True

        self.toggle_item = rumps.MenuItem('Pause Sync', callback=self.toggle_sync)
        self.sync_now_item = rumps.MenuItem('Sync Now', callback=self.sync_now)

        # Recent syncs submenu
        self.history_menu = rumps.MenuItem('Recent Syncs')
        self._rebuild_history_menu()

        self.configure_item = rumps.MenuItem('Configure...', callback=self.open_configure)

        self.open_log_item = rumps.MenuItem('View Log', callback=self.open_log)
        self.about_menu = rumps.MenuItem('About')
        about_version = rumps.MenuItem(f'FolderSync v{APP_VERSION} ({APP_BUILD_HASH})')
        about_version.set_callback(None)
        about_build = rumps.MenuItem(f'Built: {APP_BUILD_TIME}')
        about_build.set_callback(None)
        about_author = rumps.MenuItem(f'By {APP_AUTHOR}')
        about_author.set_callback(None)
        about_license = rumps.MenuItem('License: AGPL-3.0')
        about_license.set_callback(None)
        about_copyright = rumps.MenuItem(f'Copyright (C) 2026 {APP_AUTHOR}')
        about_copyright.set_callback(None)
        about_website = rumps.MenuItem('Website', callback=lambda _: subprocess.Popen(['open', APP_WEBSITE]))
        about_sponsor = rumps.MenuItem('Sponsor', callback=lambda _: subprocess.Popen(['open', APP_SPONSOR]))
        self.about_menu[about_version.title] = about_version
        self.about_menu[about_build.title] = about_build
        self.about_menu[about_author.title] = about_author
        self.about_menu[about_license.title] = about_license
        self.about_menu[about_copyright.title] = about_copyright
        self.about_menu[about_website.title] = about_website
        self.about_menu[about_sponsor.title] = about_sponsor
        self.uninstall_item = rumps.MenuItem('Uninstall...', callback=self.uninstall)
        self.quit_item = rumps.MenuItem('Quit', callback=self.quit_app)

        self.menu = [
            self.status_item,
            self.last_sync_item,
            self.next_sync_item,
            self.progress_item,
            None,
            self.sync_now_item,
            self.toggle_item,
            None,
            self.history_menu,
            self.configure_item,
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
        # Use text symbols that render as black in the menu bar (template-style)
        icons = {
            'idle': '⇅',
            'syncing': '↻',
            'error': '⚠',
            'paused': '❙❙',
        }
        self.title = icons.get(self.status, '⇅')

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
        if not self.config['enabled']:
            self.next_sync_item.title = 'Next sync: paused'
        elif self.status == 'syncing':
            self.next_sync_item.title = 'Next sync: now'
        elif self._next_sync_time:
            self.next_sync_item.title = f'Next sync: {self._next_sync_time.strftime("%b %d, %H:%M")}'
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

    def _update_progress_line(self):
        """Build a single progress line from current state. Called every second from _poll_ui."""
        if self.status != 'syncing':
            self.progress_item.hidden = True
            return
        self.progress_item.hidden = False
        elapsed = int((datetime.now() - self._sync_start_time).total_seconds()) if self._sync_start_time else 0
        elapsed_str = self._format_duration(elapsed)
        progress = self.progress
        # Track max bytes total (grows as rclone discovers files)
        if progress.bytes_total:
            if self._initial_bytes_total is None:
                self._initial_bytes_total = progress.bytes_total
            elif self._parse_bytes(progress.bytes_total) > self._parse_bytes(self._initial_bytes_total):
                self._initial_bytes_total = progress.bytes_total
        if progress.checks_total > 0:
            self.progress_item.title = f'Checking {progress.checks_done}/{progress.checks_total} files — {elapsed_str}'
        elif progress.bytes_transferred and self._initial_bytes_total:
            self.progress_item.title = f'{progress.bytes_transferred} / {self._initial_bytes_total} ({progress.percent}%) — {elapsed_str}'
        else:
            self.progress_item.title = f'Scanning... — {elapsed_str}'

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
        self.progress_item.title = 'Scanning...'
        self._initial_bytes_total = None

    def _poll_ui(self, _):
        """Called every second on the main thread by rumps.Timer. Safe to update UI here."""
        # Detect wake from sleep: if next_sync_time is in the past but sync loop
        # is still waiting (Event.wait uses monotonic clock which pauses during sleep),
        # wake it up so it syncs immediately.
        if self._next_sync_time and self._next_sync_time < datetime.now() and self.status != 'syncing' and self.config['enabled']:
            self._wake_event.set()

        # Update progress line every second (independent of rclone output)
        self._update_progress_line()

        if self._rebuild_history:
            self._rebuild_history = False
            self._rebuild_history_menu()

        if self._ui_dirty:
            self._ui_dirty = False
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

    def _load_sync_end_time(self) -> datetime | None:
        raw = self.config.get('last_sync_end_time')
        if raw:
            try:
                return datetime.fromisoformat(raw)
            except (ValueError, TypeError):
                pass
        return None

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
        """Wait for `remaining` seconds, handling wake events (config changes / sync now).
        Returns True if we should sync, False if stopped."""
        self._wake_event.clear()
        # Check if next_sync_time already passed (e.g. sync_now called before we entered wait)
        if self._next_sync_time and self._next_sync_time <= datetime.now():
            return not self.stop_event.is_set()
        while remaining > 0 and not self.stop_event.is_set():
            if self._wake_event.wait(timeout=remaining):
                self._wake_event.clear()
                # Check if next_sync_time is now or in the past (sync_now or wake from sleep)
                if self._next_sync_time and self._next_sync_time <= datetime.now():
                    remaining = 0
                else:
                    # Config changed — recalculate from last sync end + new interval
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
        self.config['last_sync_end_time'] = self._sync_end_time.isoformat()
        save_config(self.config)
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
            # Calculate next sync from last sync end instead of syncing immediately
            interval = self.config['interval_minutes'] * 60
            if self._sync_end_time:
                since_last = (datetime.now() - self._sync_end_time).total_seconds()
                remaining = max(0, interval - since_last)
            else:
                remaining = 0  # no previous sync — sync now
            self._save_next_sync_time(datetime.now() + timedelta(seconds=remaining))
            self.start_sync_loop()
        else:
            self.status = 'paused'
            self._save_next_sync_time(None)
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

    def open_configure(self, _):
        """Open the native preferences window."""
        if hasattr(self, '_config_window') and self._config_window is not None:
            self._config_window.window.makeKeyAndOrderFront_(None)
            NSApp.activateIgnoringOtherApps_(True)
            return
        self._config_window = _ConfigWindow(self)

    def _apply_config(self, source, destination, interval_minutes, autostart):
        """Called by the config window when Save is clicked."""
        changed = False
        if source != self.config['source']:
            self.config['source'] = source
            changed = True
        if destination != self.config['destination']:
            self.config['destination'] = destination
            changed = True
        if interval_minutes != self.config['interval_minutes']:
            self.config['interval_minutes'] = interval_minutes
            changed = True
        if changed:
            save_config(self.config)
            self._wake_event.set()
            rumps.notification('FolderSync', 'Config saved', 'Settings updated.', sound=False)
        # Handle autostart toggle
        currently_installed = is_launchd_installed()
        if autostart and not currently_installed:
            if install_launchd_plist():
                rumps.notification('FolderSync', 'Auto-start enabled', 'App will start automatically on login.', sound=False)
            else:
                rumps.notification('FolderSync', 'Error', 'App not found in /Applications. Install first.', sound=False)
        elif not autostart and currently_installed:
            uninstall_launchd_plist()
            rumps.notification('FolderSync', 'Auto-start disabled', 'App will no longer start on login.', sound=False)

    def open_log(self, _):
        log = os.path.expanduser('~/foldersync.log')
        if os.path.exists(log):
            escaped_log = log.replace('\\', '\\\\').replace('"', '\\"').replace("'", "\\'")
            script = f'tell application "Terminal"\n  activate\n  do script "tail -n 80 -f \'{escaped_log}\'"\nend tell'
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
