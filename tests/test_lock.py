from inbox_janitor.lock import RunLock


def test_only_one_holder_at_a_time(tmp_path):
    first, second = RunLock(tmp_path / ".lock"), RunLock(tmp_path / ".lock")
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_the_lock_works_as_a_context_manager(tmp_path):
    with RunLock(tmp_path / ".lock") as held:
        assert held
        assert not RunLock(tmp_path / ".lock").acquire()
    assert RunLock(tmp_path / ".lock").acquire()


def test_the_lock_creates_its_directory(tmp_path):
    lock = RunLock(tmp_path / "new" / "dir" / ".lock")
    assert lock.acquire()
    lock.release()
