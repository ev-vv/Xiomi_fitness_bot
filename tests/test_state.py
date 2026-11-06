from extractor.state import DAY, MAX_FDS_ATTEMPTS, LoadState

NOW = 1_760_000_000


def test_first_run_backfills(tmp_path):
    state = LoadState(tmp_path / "s.sqlite")
    assert state.window("heart_rate", NOW, backfill_days=30, overlap_days=2) == (NOW - 30 * DAY, NOW)


def test_window_restarts_from_last_data_minus_overlap(tmp_path):
    state = LoadState(tmp_path / "s.sqlite")
    state.commit("heart_rate", max_ts=NOW - 10 * DAY, synced_until=NOW - DAY)
    assert state.window("heart_rate", NOW, 30, 2) == (NOW - 12 * DAY, NOW)


def test_empty_stream_uses_synced_until(tmp_path):
    state = LoadState(tmp_path / "s.sqlite")
    state.commit("stress", max_ts=None, synced_until=NOW - DAY)
    assert state.window("stress", NOW, 30, 2) == (NOW - 3 * DAY, NOW)


def test_watermark_never_moves_backwards(tmp_path):
    state = LoadState(tmp_path / "s.sqlite")
    state.commit("spo2", max_ts=NOW, synced_until=NOW)
    state.commit("spo2", max_ts=NOW - 5 * DAY, synced_until=None)
    mark = state.get("spo2")
    assert mark.last_ts == NOW and mark.synced_until == NOW


def test_window_capped(tmp_path):
    state = LoadState(tmp_path / "s.sqlite")
    state.commit("heart_rate", max_ts=NOW - 1000 * DAY)
    assert state.window("heart_rate", NOW, 30, 2, max_days=365)[0] == NOW - 365 * DAY


def test_cursor_kept_when_not_given(tmp_path):
    state = LoadState(tmp_path / "s.sqlite")
    state.commit("workouts", cursor="42")
    state.commit("workouts", max_ts=NOW)
    assert state.get("workouts").last_cursor == "42"


def test_fds_ledger_retries_then_gives_up(tmp_path):
    state = LoadState(tmp_path / "s.sqlite")
    assert state.fds_needed(NOW)
    for _ in range(MAX_FDS_ATTEMPTS):
        state.mark_fds(NOW, "failed")
    assert not state.fds_needed(NOW)
    state.mark_fds(NOW + 1, "ok")
    assert not state.fds_needed(NOW + 1)
