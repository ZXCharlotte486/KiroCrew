"""Merge restore resolves each core-file name once.

The memory, memory-index and cron core files used to be checked by name
(``is_file()``) and then copied by name (``shutil.copy2``), and the cron merge
read the live ``crons.json`` by name and rewrote it by name. Each is two
resolutions of one string: a dangling link at the live name passes
``not is_file()`` and is then followed by the copy, a hardlink alias in the
agent-writable staging tree is a regular file every name check accepts, and a
link swapped in between the cron read and rewrite redirects the rewrite.

Every fixture is a real link or hardlink on a real filesystem.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import portability
from kiro_crew import snapshot as snapshot_mod
from kiro_crew import snapshot_merge

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX link fixtures")

SECRET = b"AWS_SECRET_STAND_IN=hunter2\n"


def _dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    snap, mc, outside = tmp_path / "snap", tmp_path / "home", tmp_path / "outside"
    for d in (snap, mc, outside):
        d.mkdir()
    return snap, mc, outside


def _crons(*names: str) -> str:
    return json.dumps({"jobs": [{"name": n, "id": n} for n in names]})


# ── Sites 1-3: memory.db, memory_index.db and crons.json install-if-absent ──


@requires_symlinks
@pytest.mark.parametrize("name,component", [("memory.db", "memory"), ("crons.json", "crons")])
def test_a_dangling_link_at_the_live_name_is_not_followed(tmp_path, name, component):
    snap, mc, outside = _dirs(tmp_path)
    (snap / name).write_bytes(b"bundle bytes" if name.endswith(".db") else _crons("a").encode())
    target = outside / "planted"
    (mc / name).symlink_to(target)

    snapshot_mod._do_merge(snap, mc, [component])

    assert not target.exists(), "the copy followed a dangling link out of the data home"
    assert (mc / name).is_symlink(), "the planted entry is left as it was"


@requires_symlinks
def test_the_index_is_not_installed_through_a_dangling_link(tmp_path):
    snap, mc, outside = _dirs(tmp_path)
    (snap / "memory.db").write_bytes(b"db")
    (snap / "memory_index.db").write_bytes(b"index")
    target = outside / "planted-index"
    (mc / "memory_index.db").symlink_to(target)

    snapshot_mod._do_merge(snap, mc, ["memory"])

    assert (mc / "memory.db").read_bytes() == b"db"
    assert not target.exists(), "the index copy followed a dangling link"


@pytest.mark.parametrize("name,component", [("memory.db", "memory"), ("crons.json", "crons")])
def test_a_hardlink_alias_in_staging_is_not_installed(tmp_path, capsys, name, component):
    snap, mc, outside = _dirs(tmp_path)
    secret = outside / "credential"
    secret.write_bytes(SECRET)
    os.link(secret, snap / name)

    snapshot_mod._do_merge(snap, mc, [component])

    assert not (mc / name).exists(), "a hardlink alias's bytes were installed as a core file"
    assert "not restored" in capsys.readouterr().out


@requires_symlinks
def test_a_symlink_in_staging_is_not_installed(tmp_path):
    snap, mc, outside = _dirs(tmp_path)
    secret = outside / "credential"
    secret.write_bytes(SECRET)
    (snap / "memory.db").symlink_to(secret)

    snapshot_mod._do_merge(snap, mc, ["memory"])

    assert not (mc / "memory.db").exists()


def test_absent_core_files_are_still_installed_with_their_mode(tmp_path, capsys):
    snap, mc, _ = _dirs(tmp_path)
    (snap / "memory.db").write_bytes(b"db")
    (snap / "memory_index.db").write_bytes(b"index")
    (snap / "crons.json").write_text(_crons("a"), encoding="utf-8")
    os.chmod(snap / "crons.json", 0o640)

    snapshot_mod._do_merge(snap, mc, ["memory", "crons"])

    assert (mc / "memory.db").read_bytes() == b"db"
    assert (mc / "memory_index.db").read_bytes() == b"index"
    assert json.loads((mc / "crons.json").read_text())["jobs"][0]["name"] == "a"
    assert (mc / "crons.json").stat().st_mode & 0o777 == 0o640
    out = capsys.readouterr().out
    assert "Memory: copied" in out and "Crons: copied" in out
    assert "✅ memory" in out and "✅ crons" in out


@requires_symlinks
def test_the_dashboard_import_twins_refuse_a_dangling_link_too(tmp_path, monkeypatch):
    """`portability`'s merge branch carries the same copies; one helper serves both."""
    snap, mc, outside = _dirs(tmp_path)
    (snap / "hooks.json").write_text("{}", encoding="utf-8")
    target = outside / "planted"
    (mc / "hooks.json").symlink_to(target)

    assert portability._install_core_file_if_absent(snap / "hooks.json", mc / "hooks.json") is False
    assert not target.exists()


# ── Site 5: _merge_crons reads and rewrites the live store ──


def _cron_pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    snap, mc, outside = _dirs(tmp_path)
    src, dst = snap / "crons.json", mc / "crons.json"
    src.write_text(_crons("imported"), encoding="utf-8")
    dst.write_text(_crons("local"), encoding="utf-8")
    return src, dst, outside


@requires_symlinks
def test_a_live_store_swapped_for_a_link_between_read_and_rewrite_is_not_written_through(
    tmp_path, monkeypatch
):
    """The exact window: the swap lands after both reads, before the rewrite.

    ``_usable_cron_shape`` runs on the destination after it has been read and before
    anything is written, so swapping there is the last instant the old code's by-name
    ``write_text`` would still follow.
    """
    src, dst, outside = _cron_pair(tmp_path)
    victim = outside / "victim.json"
    victim.write_bytes(SECRET)
    real = snapshot_merge._usable_cron_shape

    def swapping(parsed, path):
        if Path(path) == dst:
            dst.unlink()
            dst.symlink_to(victim)
        return real(parsed, path)

    monkeypatch.setattr(snapshot_merge, "_usable_cron_shape", swapping)
    assert snapshot_merge._merge_crons(src, dst) is False
    assert victim.read_bytes() == SECRET, "the rewrite followed a link swapped in after the read"


def test_a_hardlinked_live_store_is_refused(tmp_path):
    src, dst, outside = _cron_pair(tmp_path)
    alias = outside / "alias.json"
    os.link(dst, alias)
    before = alias.read_bytes()

    assert snapshot_merge._merge_crons(src, dst) is False
    assert alias.read_bytes() == before, "the merge rewrote a file it was not pointed at"


@requires_symlinks
def test_a_staged_cron_store_that_is_a_link_is_not_imported(tmp_path):
    src, dst, outside = _cron_pair(tmp_path)
    other = outside / "someone-elses-crons.json"
    other.write_text(_crons("foreign"), encoding="utf-8")
    src.unlink()
    src.symlink_to(other)
    before = dst.read_bytes()

    assert snapshot_merge._merge_crons(src, dst) is False
    assert dst.read_bytes() == before


def test_an_unswapped_merge_imports_and_keeps_the_store_regular_with_its_mode(tmp_path):
    src, dst, _ = _cron_pair(tmp_path)
    os.chmod(dst, 0o640)

    assert snapshot_merge._merge_crons(src, dst) is True
    names = [j["name"] for j in json.loads(dst.read_text())["jobs"]]
    assert names == ["local", "imported"]
    assert not dst.is_symlink()
    assert dst.stat().st_mode & 0o777 == 0o640
