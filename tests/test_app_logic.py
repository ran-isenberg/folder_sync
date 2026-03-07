"""Tests for app.py logic — _parse_bytes, _format_speed, menu states, next sync time."""

import json
import threading
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch


class TestParseBytes:
    """Test FolderSyncApp._parse_bytes static method."""

    @staticmethod
    def _parse(s):
        # Import here to avoid rumps/AppKit import issues in CI
        from app import FolderSyncApp

        return FolderSyncApp._parse_bytes(s)

    def test_parse_bytes_gib(self):
        assert self._parse('1.234 GiB') == 1.234 * 1024**3

    def test_parse_bytes_mib(self):
        assert self._parse('500.0 MiB') == 500.0 * 1024**2

    def test_parse_bytes_kib(self):
        assert self._parse('100 KiB') == 100 * 1024

    def test_parse_bytes_b(self):
        assert self._parse('42 B') == 42.0

    def test_parse_bytes_tib(self):
        assert self._parse('2.0 TiB') == 2.0 * 1024**4

    def test_parse_bytes_zero(self):
        assert self._parse('0 B') == 0.0

    def test_parse_bytes_invalid_unit(self):
        assert self._parse('100 XX') == 0.0

    def test_parse_bytes_empty_string(self):
        assert self._parse('') == 0.0

    def test_parse_bytes_no_unit(self):
        assert self._parse('12345') == 0.0

    def test_parse_bytes_non_numeric(self):
        assert self._parse('abc GiB') == 0.0


class TestFormatSpeed:
    """Test FolderSyncApp._format_speed static method."""

    @staticmethod
    def _fmt(bps):
        from app import FolderSyncApp

        return FolderSyncApp._format_speed(bps)

    def test_format_speed_gib(self):
        result = self._fmt(2.5 * 1024**3)
        assert 'GiB/s' in result
        assert '2.5' in result

    def test_format_speed_mib(self):
        result = self._fmt(10.0 * 1024**2)
        assert 'MiB/s' in result
        assert '10.0' in result

    def test_format_speed_kib(self):
        result = self._fmt(512 * 1024)
        assert 'KiB/s' in result
        assert '512.0' in result

    def test_format_speed_bytes(self):
        result = self._fmt(100)
        assert 'B/s' in result
        assert '100' in result

    def test_format_speed_zero(self):
        result = self._fmt(0)
        assert 'B/s' in result
        assert '0' in result

    def test_format_speed_boundary_mib(self):
        # Exactly 1 MiB/s
        result = self._fmt(1024**2)
        assert 'MiB/s' in result

    def test_format_speed_boundary_gib(self):
        # Exactly 1 GiB/s
        result = self._fmt(1024**3)
        assert 'GiB/s' in result


class TestFormatDuration:
    """Test FolderSyncApp._format_duration static method."""

    @staticmethod
    def _fmt(seconds):
        from app import FolderSyncApp

        return FolderSyncApp._format_duration(seconds)

    def test_seconds_only(self):
        assert self._fmt(45) == '45s'

    def test_zero(self):
        assert self._fmt(0) == '0s'

    def test_minutes_and_seconds(self):
        assert self._fmt(90) == '1m30s'

    def test_exact_minutes(self):
        assert self._fmt(300) == '5m0s'

    def test_hours_and_minutes(self):
        assert self._fmt(3900) == '1h5m'

    def test_large_duration(self):
        assert self._fmt(7200) == '2h0m'


class TestUpdateMenuStates:
    """Test button enable/disable logic in update_menu."""

    def _make_app(self):
        """Create a FolderSyncApp with mocked rumps to avoid GUI initialization."""
        from app import FolderSyncApp

        with patch('app.rumps'), patch('app.is_launchd_installed', return_value=False), patch('app.load_config', return_value={'source': '/src', 'destination': '/dst', 'interval_minutes': 5, 'enabled': True, 'use_checksum': True}), patch('app.load_history', return_value=[]), patch('app.find_rclone', return_value='/usr/bin/rclone'):
            # Mock rumps.App.__init__
            with patch.object(FolderSyncApp, '__init__', lambda self: None):
                app = FolderSyncApp()
                # Set up minimum required attributes
                app.config = {'source': '/src', 'destination': '/dst', 'interval_minutes': 5, 'enabled': True, 'use_checksum': True}
                app.status = 'idle'
                app.last_sync = None
                app.last_error = None
                app.sync_thread = None
                app._next_sync_time = None
                app.title = ''

                # Create mock menu items
                app.status_item = MagicMock()
                app.last_sync_item = MagicMock()
                app.next_sync_item = MagicMock()
                app.toggle_item = MagicMock()
                app.sync_now_item = MagicMock()
                app.progress_item = MagicMock()
                return app

    def test_pause_disabled_when_no_sync_loop(self):
        app = self._make_app()
        app.sync_thread = None
        app.config['enabled'] = True
        app.update_menu()
        # No active loop and not paused → Pause Sync disabled (callback=None)
        app.toggle_item.set_callback.assert_called_with(None)

    def test_pause_enabled_when_syncing(self):
        app = self._make_app()
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        app.sync_thread = mock_thread
        app.config['enabled'] = True
        app.status = 'syncing'
        app.update_menu()
        assert app.toggle_item.title == 'Pause Sync'
        app.toggle_item.set_callback.assert_called_with(app.toggle_sync)

    def test_pause_grayed_out_when_idle(self):
        app = self._make_app()
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        app.sync_thread = mock_thread
        app.config['enabled'] = True
        app.status = 'idle'
        app.update_menu()
        assert app.toggle_item.title == 'Pause Sync'
        app.toggle_item.set_callback.assert_called_with(None)

    def test_resume_shown_when_paused(self):
        app = self._make_app()
        app.config['enabled'] = False
        app.sync_thread = None
        app.update_menu()
        assert app.toggle_item.title == 'Resume Sync'
        app.toggle_item.set_callback.assert_called_with(app.toggle_sync)

    def test_sync_now_disabled_when_paused(self):
        app = self._make_app()
        app.config['enabled'] = False
        app.update_menu()
        app.sync_now_item.set_callback.assert_called_with(None)

    def test_sync_now_disabled_when_syncing(self):
        app = self._make_app()
        app.status = 'syncing'
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        app.sync_thread = mock_thread
        app.update_menu()
        app.sync_now_item.set_callback.assert_called_with(None)

    def test_sync_now_enabled_when_idle_with_active_loop(self):
        app = self._make_app()
        app.status = 'idle'
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        app.sync_thread = mock_thread
        app.config['enabled'] = True
        app.update_menu()
        app.sync_now_item.set_callback.assert_called_with(app.sync_now)


class TestNextSyncTime:
    """Test next sync time display logic."""

    def _make_app(self):
        from app import FolderSyncApp

        with patch.object(FolderSyncApp, '__init__', lambda self: None):
            app = FolderSyncApp()
            app.config = {'source': '/src', 'destination': '/dst', 'interval_minutes': 5, 'enabled': True, 'use_checksum': True}
            app.status = 'idle'
            app.last_sync = None
            app.last_error = None
            app.sync_thread = None
            app._next_sync_time = None
            app.title = ''
            app.status_item = MagicMock()
            app.last_sync_item = MagicMock()
            app.next_sync_item = MagicMock()
            app.toggle_item = MagicMock()
            app.sync_now_item = MagicMock()
            app.progress_item = MagicMock()
            return app

    def test_next_sync_shows_time_when_set(self):
        app = self._make_app()
        app._next_sync_time = datetime(2026, 3, 6, 14, 30)
        app.config['enabled'] = True
        app.update_menu()
        assert 'Mar 06, 14:30' in app.next_sync_item.title

    def test_next_sync_shows_now_when_syncing(self):
        app = self._make_app()
        app.status = 'syncing'
        app._next_sync_time = datetime(2026, 3, 6, 14, 30)
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        app.sync_thread = mock_thread
        app.update_menu()
        assert app.next_sync_item.title == 'Next sync: now'

    def test_next_sync_shows_paused_when_disabled(self):
        app = self._make_app()
        app.config['enabled'] = False
        app._next_sync_time = datetime(2026, 3, 6, 14, 30)
        app.update_menu()
        assert app.next_sync_item.title == 'Next sync: paused'

    def test_next_sync_shows_dash_when_no_time(self):
        app = self._make_app()
        app._next_sync_time = None
        app.update_menu()
        assert app.next_sync_item.title == 'Next sync: —'


class TestStatusDisplay:
    """Test status label rendering."""

    def _make_app(self):
        from app import FolderSyncApp

        with patch.object(FolderSyncApp, '__init__', lambda self: None):
            app = FolderSyncApp()
            app.config = {'source': '/src', 'destination': '/dst', 'interval_minutes': 5, 'enabled': True, 'use_checksum': True}
            app.status = 'idle'
            app.last_sync = None
            app.last_error = None
            app.sync_thread = None
            app._next_sync_time = None
            app.title = ''
            app.status_item = MagicMock()
            app.last_sync_item = MagicMock()
            app.next_sync_item = MagicMock()
            app.toggle_item = MagicMock()
            app.sync_now_item = MagicMock()
            app.progress_item = MagicMock()
            return app

    def test_idle_status(self):
        app = self._make_app()
        app.status = 'idle'
        app.update_menu()
        assert 'Idle' in app.status_item.title

    def test_syncing_status(self):
        app = self._make_app()
        app.status = 'syncing'
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        app.sync_thread = mock_thread
        app.update_menu()
        assert 'Syncing' in app.status_item.title

    def test_error_status_includes_message(self):
        app = self._make_app()
        app.status = 'error'
        app.last_error = 'NAS not mounted'
        app.update_menu()
        assert 'NAS not mounted' in app.status_item.title

    def test_paused_status(self):
        app = self._make_app()
        app.status = 'paused'
        app.config['enabled'] = False
        app.update_menu()
        assert 'Paused' in app.status_item.title

    def test_last_sync_never(self):
        app = self._make_app()
        app.last_sync = None
        app.update_menu()
        assert 'Never' in app.last_sync_item.title

    def test_last_sync_shows_timestamp(self):
        app = self._make_app()
        app.last_sync = 'Mar 06, 14:30'
        app.update_menu()
        assert 'Mar 06, 14:30' in app.last_sync_item.title


class TestNextSyncTimePersistence:
    """Test saving/loading next_sync_time to/from config."""

    def _make_app(self):
        from app import FolderSyncApp

        with patch.object(FolderSyncApp, '__init__', lambda self: None):
            app = FolderSyncApp()
            app.config = {'source': '/src', 'destination': '/dst', 'interval_minutes': 5, 'enabled': True, 'use_checksum': True}
            app.status = 'idle'
            app.last_sync = None
            app.last_error = None
            app.sync_thread = None
            app._next_sync_time = None
            app._sync_start_time = None
            app._sync_end_time = None
            app._rclone_proc = None
            app._initial_bytes_total = None
            app.title = ''
            app.stop_event = threading.Event()
            app._wake_event = threading.Event()
            app.status_item = MagicMock()
            app.last_sync_item = MagicMock()
            app.next_sync_item = MagicMock()
            app.toggle_item = MagicMock()
            app.sync_now_item = MagicMock()
            app.progress_item = MagicMock()
            return app

    def test_load_next_sync_time_none_when_not_in_config(self):
        app = self._make_app()
        assert app._load_next_sync_time() is None

    def test_load_next_sync_time_from_config(self):
        app = self._make_app()
        future = datetime(2026, 3, 7, 10, 30)
        app.config['next_sync_time'] = future.isoformat()
        result = app._load_next_sync_time()
        assert result == future

    def test_load_next_sync_time_invalid_value(self):
        app = self._make_app()
        app.config['next_sync_time'] = 'not-a-date'
        assert app._load_next_sync_time() is None

    def test_load_next_sync_time_none_value(self):
        app = self._make_app()
        app.config['next_sync_time'] = None
        assert app._load_next_sync_time() is None

    def test_save_next_sync_time_persists(self):
        app = self._make_app()
        future = datetime(2026, 3, 7, 10, 30)
        with patch('app.save_config') as mock_save:
            app._save_next_sync_time(future)
        assert app._next_sync_time == future
        assert app.config['next_sync_time'] == future.isoformat()
        mock_save.assert_called_once_with(app.config)

    def test_save_next_sync_time_clears_when_none(self):
        app = self._make_app()
        app.config['next_sync_time'] = '2026-03-07T10:30:00'
        with patch('app.save_config') as mock_save:
            app._save_next_sync_time(None)
        assert app._next_sync_time is None
        assert app.config['next_sync_time'] is None
        mock_save.assert_called_once()

    def test_clean_install_no_next_sync_time(self):
        """Clean install: no config file, no next_sync_time → returns None from load."""
        app = self._make_app()
        # Default config has no next_sync_time key
        assert app._load_next_sync_time() is None

    def test_fresh_install_writes_next_sync_time_to_config(self):
        """On fresh install (no saved next_sync_time), init writes it to config as now for immediate sync."""
        app = self._make_app()
        app._next_sync_time = None  # simulate fresh install
        with patch('app.save_config'):
            app._save_next_sync_time(datetime.now())
        assert app._next_sync_time is not None
        assert app.config['next_sync_time'] is not None
        # Should be approximately now (within 2 seconds)
        delta = abs((app._next_sync_time - datetime.now()).total_seconds())
        assert delta < 2

    def test_config_file_round_trip(self, tmp_path):
        """next_sync_time survives save→load cycle via config file."""
        from sync import load_config, save_config

        config_file = str(tmp_path / 'config.json')
        future = datetime(2026, 3, 7, 10, 30)
        config = {'source': '/src', 'destination': '/dst', 'interval_minutes': 5, 'enabled': True, 'use_checksum': True, 'next_sync_time': future.isoformat()}
        save_config(config, config_file)

        loaded = load_config(config_file)
        assert loaded['next_sync_time'] == future.isoformat()
        assert datetime.fromisoformat(loaded['next_sync_time']) == future

    def test_config_file_without_next_sync_time(self, tmp_path):
        """Old config without next_sync_time loads without error."""
        from sync import load_config

        config_file = str(tmp_path / 'config.json')
        with open(config_file, 'w') as f:
            json.dump({'source': '/src', 'destination': '/dst', 'interval_minutes': 5, 'enabled': True}, f)

        loaded = load_config(config_file)
        assert loaded.get('next_sync_time') is None

    def test_interval_change_updates_next_sync_time(self):
        """When interval changes and wake event fires, next sync time is recalculated."""
        app = self._make_app()
        app._sync_end_time = datetime.now() - timedelta(minutes=2)
        with patch('app.save_config'):
            # Simulate: interval was 5 min, changed to 10 min
            app.config['interval_minutes'] = 10
            app._save_next_sync_time(datetime.now() + timedelta(minutes=8))
        assert app._next_sync_time is not None
        # Next sync should be ~8 minutes from now (10 min interval - 2 min since sync ended)
        delta = (app._next_sync_time - datetime.now()).total_seconds()
        assert 7 * 60 < delta < 9 * 60

    def test_past_next_sync_time_triggers_immediate_sync(self):
        """If loaded next_sync_time is in the past, sync loop should sync immediately."""
        app = self._make_app()
        # Set next sync time to 10 minutes ago
        app._next_sync_time = datetime.now() - timedelta(minutes=10)
        # The condition in _sync_loop checks: if self._next_sync_time > datetime.now()
        # Past time means this is False, so it falls through to immediate sync
        assert app._next_sync_time < datetime.now()

    def test_future_next_sync_time_waits(self):
        """If loaded next_sync_time is in the future, remaining wait time is calculated."""
        app = self._make_app()
        app._next_sync_time = datetime.now() + timedelta(minutes=3)
        remaining = (app._next_sync_time - datetime.now()).total_seconds()
        assert 2 * 60 < remaining < 4 * 60

    def test_quit_preserves_next_sync_time(self, tmp_path):
        """When the app quits, next_sync_time stays in config for restart."""
        from sync import load_config, save_config

        config_file = str(tmp_path / 'config.json')
        future = datetime(2026, 3, 7, 10, 30)
        config = {'source': '/src', 'destination': '/dst', 'interval_minutes': 5, 'enabled': True, 'use_checksum': True, 'next_sync_time': future.isoformat()}
        save_config(config, config_file)

        # Simulate quit: stop_event is set but config should still have next_sync_time
        loaded = load_config(config_file)
        assert loaded['next_sync_time'] == future.isoformat()

    def test_pause_clears_next_sync_time(self):
        """When the user pauses sync, next_sync_time is cleared."""
        app = self._make_app()
        future = datetime(2026, 3, 7, 10, 30)
        app._next_sync_time = future
        app.config['next_sync_time'] = future.isoformat()
        with patch('app.save_config'):
            app._save_next_sync_time(None)
        assert app._next_sync_time is None
        assert app.config['next_sync_time'] is None

    def test_sync_now_sets_next_sync_time_to_now(self):
        """sync_now should set next_sync_time to now and wake the sync loop."""
        app = self._make_app()
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        app.sync_thread = mock_thread
        app.status = 'idle'
        app.config['enabled'] = True
        app._wake_event = threading.Event()

        with patch('app.save_config'):
            app.sync_now(None)

        assert app._next_sync_time is not None
        # next_sync_time should be approximately now (within 2 seconds)
        delta = abs((app._next_sync_time - datetime.now()).total_seconds())
        assert delta < 2
        assert app._wake_event.is_set()

    def test_sync_now_ignored_when_syncing(self):
        """sync_now should do nothing if already syncing."""
        app = self._make_app()
        app.status = 'syncing'
        app._wake_event = threading.Event()
        app.sync_thread = MagicMock()

        app.sync_now(None)

        assert not app._wake_event.is_set()

    def test_sync_now_starts_thread_when_no_loop(self):
        """sync_now should start a standalone thread if no sync loop is running."""
        app = self._make_app()
        app.status = 'idle'
        app.sync_thread = None
        app._wake_event = threading.Event()

        with patch('threading.Thread') as mock_thread:
            mock_instance = MagicMock()
            mock_thread.return_value = mock_instance
            app.sync_now(None)
            mock_thread.assert_called_once()
            mock_instance.start.assert_called_once()

    def test_sync_now_enabled_in_menu_when_idle_with_loop(self):
        """Sync Now should be clickable when idle with an active sync loop."""
        app = self._make_app()
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        app.sync_thread = mock_thread
        app.config['enabled'] = True
        app.status = 'idle'
        app.update_menu()
        app.sync_now_item.set_callback.assert_called_with(app.sync_now)

    def test_sync_now_disabled_in_menu_when_syncing(self):
        """Sync Now should be grayed out when a sync is in progress."""
        app = self._make_app()
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        app.sync_thread = mock_thread
        app.config['enabled'] = True
        app.status = 'syncing'
        app.update_menu()
        app.sync_now_item.set_callback.assert_called_with(None)

    def test_wait_until_next_sync_respects_sync_now(self):
        """_wait_until_next_sync should return immediately when next_sync_time is in the past."""
        app = self._make_app()
        app._next_sync_time = datetime.now() - timedelta(seconds=1)
        app._wake_event = threading.Event()

        result = app._wait_until_next_sync(300)

        assert result is True  # should sync (not stopped)

    def test_next_sync_time_survives_app_restart(self, tmp_path):
        """Full restart scenario: save time, reload config, verify time is there."""
        from sync import load_config, save_config

        config_file = str(tmp_path / 'config.json')
        future = datetime(2026, 3, 7, 14, 0)

        # App running: saves next sync time
        config = {'source': '/src', 'destination': '/dst', 'interval_minutes': 5, 'enabled': True, 'use_checksum': True}
        config['next_sync_time'] = future.isoformat()
        save_config(config, config_file)

        # App restarts: loads config and recovers next sync time
        loaded = load_config(config_file)
        recovered = datetime.fromisoformat(loaded['next_sync_time'])
        assert recovered == future
