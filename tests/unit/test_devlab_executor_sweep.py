import shutil
from pathlib import Path

from sa_home_bot.config import DevlabConfig
from sa_home_bot.devlab.executor.sweep import remove_foreign_entries, sweep_orphans
from sa_home_bot.devlab.executor.wrapper import WrapperClient
from sa_home_bot.utils.process import ProcResult


class FakeSpawn:
    def __init__(self, code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.code = code

    async def __call__(self, argv, *, input=None, timeout=None, **kwargs) -> ProcResult:
        self.calls.append(list(argv))
        return ProcResult(self.code, b"", b"")


def populate(work: Path) -> None:
    for sub, name in [
        ("jobs", "j-keep"),
        ("jobs", "j-old"),
        ("agent", "a-parked"),
        ("agent", "a-old"),
    ]:
        (work / sub / name).mkdir(parents=True)
        (work / sub / name / "f.txt").write_text("x")
    (work / "jobs" / "j-old.tar").write_text("x")
    (work / "state.json").write_text("{}")
    (work / "agent-state.json").write_text("{}")
    (work / "mirrors").mkdir()


def test_remove_foreign_entries_keeps_held_and_other_files(tmp_path):
    populate(tmp_path)
    removed = remove_foreign_entries(tmp_path, {"j-keep", "a-parked"})
    assert removed == 3
    assert sorted(p.name for p in (tmp_path / "jobs").iterdir()) == ["j-keep"]
    assert sorted(p.name for p in (tmp_path / "agent").iterdir()) == ["a-parked"]
    assert (tmp_path / "state.json").exists() and (tmp_path / "mirrors").is_dir()


def test_remove_foreign_entries_without_dirs(tmp_path):
    assert remove_foreign_entries(tmp_path, set()) == 0


async def test_sweep_orphans_cleans_dirs_and_calls_wrapper(tmp_path):
    populate(tmp_path)
    spawn = FakeSpawn()
    wrapper = WrapperClient(DevlabConfig(), spawn)
    await sweep_orphans(wrapper, tmp_path, ["J-keep", "a-parked", "bad id!"])
    assert not (tmp_path / "jobs" / "j-old").exists()
    assert spawn.calls[0][5:] == ["sweep", "a-parked", "j-keep"]


async def test_sweep_orphans_survives_old_wrapper(tmp_path):
    spawn = FakeSpawn(code=2)
    wrapper = WrapperClient(DevlabConfig(), spawn)
    await sweep_orphans(wrapper, tmp_path, [])
    assert spawn.calls[0][5:] == ["sweep"]


def test_agent_dir_kept_when_state_file_missing(tmp_path, caplog):
    populate(tmp_path)
    (tmp_path / "agent-state.json").unlink()
    removed = remove_foreign_entries(tmp_path, {"j-keep"})
    assert removed == 2
    assert sorted(p.name for p in (tmp_path / "agent").iterdir()) == ["a-old", "a-parked"]
    assert "каталог агента не чищу" in caplog.text


def test_empty_agent_dir_without_state_is_fine(tmp_path):
    (tmp_path / "agent").mkdir()
    assert remove_foreign_entries(tmp_path, set()) == 0


def test_failure_on_one_entry_does_not_stop_the_rest(tmp_path, monkeypatch):
    populate(tmp_path)
    real = shutil.rmtree

    def flaky(path, *args, **kwargs):
        if Path(path).name == "j-old":
            raise OSError("busy")
        real(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", flaky)
    removed = remove_foreign_entries(tmp_path, {"j-keep", "a-parked"})
    assert removed == 2
    assert (tmp_path / "jobs" / "j-old").exists()
    assert not (tmp_path / "agent" / "a-old").exists()
