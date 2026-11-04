import pytest

from followup.run_lock import RunBusy, hold_run_lock


def test_second_holder_raises_run_busy(tmp_path):
    with hold_run_lock(tmp_path), pytest.raises(RunBusy), hold_run_lock(tmp_path):
        pass


def test_lock_is_released_after_an_exception(tmp_path):
    with pytest.raises(RuntimeError), hold_run_lock(tmp_path):
        raise RuntimeError("crash inside the run")
    with hold_run_lock(tmp_path):
        pass


def test_lock_is_free_again_after_a_normal_exit(tmp_path):
    with hold_run_lock(tmp_path):
        pass
    with hold_run_lock(tmp_path):
        pass


def test_disabled_lock_admits_a_second_holder(tmp_path):
    with hold_run_lock(tmp_path), hold_run_lock(tmp_path, enabled=False):
        pass


def test_the_directory_is_created_when_missing(tmp_path):
    target = tmp_path / "nested" / "out"
    with hold_run_lock(target):
        assert (target / ".run.lock").exists()
