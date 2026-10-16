from dataclasses import replace
import json

import pytest

from vlc_monitor.models import AppConfig, AlarmEvent, RegionConfig
from vlc_monitor.notifications.local import SoundPolicy
from vlc_monitor.storage.config_store import ConfigStore, ConfigError, redacted_config
from vlc_monitor.storage.event_store import EventStore


def test_config_roundtrip_validation_and_corrupt_file_preservation(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    config = AppConfig(source="https://user:password@example.invalid/live?token=private", regions=[RegionConfig()])
    store.save(config)
    loaded = store.load()
    assert loaded == config
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        store.save(replace(config, regions=[replace(config.regions[0], black_count=0)]))
    assert store.path.read_bytes() == before
    store.path.write_text('{"broken":')
    with pytest.raises(ConfigError):
        store.load()
    assert store.path.read_text() == '{"broken":'
    assert "password" not in json.dumps(redacted_config(config))
    assert "private" not in json.dumps(redacted_config(config))


def test_schema2_legacy_wechat_repeat_cannot_reactivate(tmp_path):
    from dataclasses import asdict
    store = ConfigStore(tmp_path / 'config.json')
    legacy = asdict(AppConfig(wechat_reminders_enabled=True, wechat_repeat_seconds=120))
    store.path.write_text(json.dumps(legacy))
    loaded = store.load()
    assert loaded.schema_version == 2 and not loaded.wechat_reminders_enabled
    assert loaded.wechat_repeat_seconds == 120
    loaded.wechat_reminders_enabled = True
    store.save(loaded)
    assert not json.loads(store.path.read_text())['wechat_reminders_enabled']


def test_history_updates_same_event_and_sound_mute_does_not_affect_history(tmp_path):
    import time
    now = time.time()
    store = EventStore(tmp_path / "history.sqlite3")
    event = AlarmEvent("id", "session", "region", "测试区域", "black", 0, now, now, now, 4)
    store.upsert([event])
    store.upsert([replace(event, hit_count=5, ended_at=now + 3, end_reason="手动停止")])
    rows = store.recent()
    assert len(rows) == 1 and rows[0]["end_reason"] == "手动停止"
    assert rows[0]["hit_count"] == 5
    store.close()
    policy = SoundPolicy()
    assert policy.request(0)
    assert not policy.request(.01)
    policy.mute(1)
    assert not policy.request(300)
    assert policy.request(301)
    policy.mute(302)
    policy.unmute()
    assert policy.request(305)


def test_v1_migration_backup_and_unknown_version_preserved(tmp_path):
    from dataclasses import asdict
    store = ConfigStore(tmp_path / 'config.json')
    data = asdict(AppConfig(source='local-test', regions=[RegionConfig(id='r')]))
    for key in ('foreground_on_alarm', 'local_alert_interval_seconds', 'wechat_enabled', 'wechat_reminders_enabled', 'wechat_repeat_seconds'):
        data.pop(key)
    data['schema_version'] = 1
    store.path.write_text(json.dumps(data))
    original = store.path.read_bytes()
    loaded = store.load()
    assert loaded.schema_version == 2 and loaded.regions[0].id == 'r'
    assert loaded.foreground_on_alarm and not loaded.wechat_enabled
    assert store.path.read_bytes() == original
    store.save(loaded)
    assert store.path.with_name('config.json.v1.bak').read_bytes() == original
    store.path.write_text('{"schema_version":99}')
    with pytest.raises(ConfigError):
        store.load()
    assert store.path.read_text() == '{"schema_version":99}'


@pytest.mark.parametrize('field,value', [('local_alert_interval_seconds', 2), ('local_alert_interval_seconds', float('nan')), ('wechat_repeat_seconds', 59), ('wechat_enabled', 1)])
def test_notification_config_validation(field, value):
    with pytest.raises(ValueError):
        replace(AppConfig(), **{field: value}).validate()


def test_event_snapshot_does_not_overwrite_acknowledgement_or_delivery(tmp_path):
    import time
    now = time.time()
    store = EventStore(tmp_path / 'history.sqlite3')
    event = AlarmEvent('id', 's', 'r', 'region', 'black', 0, now, now, now, 4)
    store.upsert([event])
    store.acknowledge(['id'], now)
    store.record_delivery(['id'], 'local', 'first', 'requested')
    store.upsert([replace(event, hit_count=8)])
    row = store.recent()[0]
    assert row['acknowledged_at'] == now and row['hit_count'] == 8
    assert row['delivery']['channel'] == 'local'
    with pytest.raises(ValueError):
        store.record_delivery(['id'], 'wechat', 'first', 'raw secret response')
    store.db.execute('UPDATE events SET triggered=0')
    store.db.commit()
    store.cleanup()
    assert store.db.execute('SELECT COUNT(*) FROM alarm_attention').fetchone()[0] == 0
    assert store.db.execute('SELECT COUNT(*) FROM notification_delivery').fetchone()[0] == 0
    store.close()


def test_closed_event_cannot_reopen_after_display_cache_eviction(tmp_path):
    import time
    now = time.time()
    store = EventStore(tmp_path / 'history.sqlite3')
    event = AlarmEvent('old', 's', 'r', 'region', 'black', 0, now, now, now, 4, ended_at=now + 1, end_reason='恢复正常')
    store.upsert([event])
    store.upsert([replace(event, ended_at=None, end_reason='')])
    assert store.recent()[0]['ended_at'] == now + 1
    store.close()


@pytest.mark.parametrize('failing_fsync', [1, 2])
def test_migration_write_failure_keeps_v1_and_can_retry(tmp_path, monkeypatch, failing_fsync):
    import os
    from dataclasses import asdict
    store = ConfigStore(tmp_path / 'config.json')
    original = asdict(AppConfig(regions=[RegionConfig(id='preserved')]))
    for key in ('foreground_on_alarm', 'local_alert_interval_seconds', 'wechat_enabled', 'wechat_reminders_enabled', 'wechat_repeat_seconds'):
        original.pop(key)
    original['schema_version'] = 1
    store.path.write_text(json.dumps(original))
    before = store.path.read_bytes()
    migrated = store.load()
    real_fsync = os.fsync
    calls = 0

    def fail_one_write(fd):
        nonlocal calls
        calls += 1
        if calls == failing_fsync:
            raise OSError('synthetic disk write failure')
        real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, 'fsync', fail_one_write)
        with pytest.raises(OSError):
            store.save(migrated)
    assert store.path.read_bytes() == before
    assert not list(tmp_path.glob('.*-backup-*'))
    assert not list(tmp_path.glob('.config-*.tmp'))
    backup = store.path.with_name('config.json.v1.bak')
    assert not backup.exists() or backup.read_bytes() == before
    store.save(migrated)
    assert backup.read_bytes() == before
    assert store.load().regions[0].id == 'preserved'
