"""A cron child cannot read any app's ``.app_secret``, and still runs its own bundle.

``<config_dir>/apps/<app>/.app_secret`` is a bearer credential: whoever reads it can act
as that app against the Gateway. Both cron exec paths mask the whole apps tree, so every
app's secret is covered, including an app installed while the child runs. The bundle a
cron runs from comes back as a read-write private window (its code and sibling modules
import, its ``data/`` stays writable) with that app's secret masked inside, and listed
as a required mask so a secret moved aside before mask time refuses the spawn.

PER-WINDOW scan: before a bundle's window opens, that bundle tree is scanned for a hard
link to any app's ``.app_secret`` inode. The cron's OWN bundle holding one refuses only
that app's cron; a command-named bundle holding one withholds only that window. One
app's stray link never withholds every window.

Two layers are pinned:

* ``cron_apps_mask`` -- which paths each kind of cron asks the sandbox to hide and
  re-expose, including both spellings of a symlinked home and the per-window refusal.
  These are pure planner tests and run on every host.
* REAL cases a-f -- each actually launches a cron child under the real OS sandbox
  backend (Linux namespace / macOS Seatbelt, no mocks) and asserts the read is denied
  or the import works. They SKIP cleanly, with the reason, where no backend is
  available (a nested sandbox, Windows, or a CI runner without unprivileged user
  namespaces); the macOS CI job runs them under real Seatbelt.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from kiro_crew import cron_script
from kiro_crew.cron_script import CronAppsMask, cron_apps_mask

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="no cron sandbox backend on Windows; the launcher and its paths are POSIX-only",
)

OWN = "own-app"
OTHER = "other-app"


def _make_app(apps: Path, name: str) -> Path:
    app = apps / name
    (app / "lib").mkdir(parents=True)
    (app / "data").mkdir()
    (app / ".app_secret").write_text(f"secret-of-{name}\n")
    (app / "job.py").write_text("def run(ctx):\n    pass\n")
    (app / "backend").mkdir()
    (app / "backend" / "server.mjs").write_text("export const ok = 1;\n")
    (app / "backend" / "sibling.mjs").write_text("export const two = 2;\n")
    return app


@pytest.fixture()
def crew_home(tmp_path, monkeypatch) -> Path:
    """A crew home with two installed apps, reached through a plain (unlinked) $HOME."""
    home = tmp_path / "home"
    crew = home / ".kiro" / "crew"
    (crew / "crons").mkdir(parents=True)
    _make_app(crew / "apps", OWN)
    _make_app(crew / "apps", OTHER)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    return crew


@pytest.fixture()
def linked_crew_home(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """The same layout behind a symlinked $HOME (``/home -> /local/home``)."""
    real_home = tmp_path / "local" / "home"
    crew = real_home / ".kiro" / "crew"
    (crew / "crons").mkdir(parents=True)
    _make_app(crew / "apps", OWN)
    _make_app(crew / "apps", OTHER)
    link_home = tmp_path / "home"
    link_home.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setenv("HOME", str(link_home))
    monkeypatch.setenv("KIROCREW_HOME", str(crew.resolve()))
    return crew.resolve(), link_home / ".kiro" / "crew"


def _under(parent: str, child: str) -> bool:
    parent = os.path.normpath(parent)
    child = os.path.normpath(child)
    return child == parent or child.startswith(parent + os.sep)


def _readable(path: str, mask: CronAppsMask) -> bool:
    """Whether *path* is readable under *mask*, by the rules both backends implement.

    Masked when some hidden entry covers it and no window between that entry and the
    path re-exposes it; a hidden entry nested INSIDE a window masks again.
    """
    best_hidden = max((h for h in mask.hidden if _under(h, path)), key=len, default=None)
    if best_hidden is None:
        return True
    best_window = max((w for w in mask.windows if _under(w, path)), key=len, default=None)
    return best_window is not None and len(best_window) > len(best_hidden)


def _writable(path: str, mask: CronAppsMask) -> bool:
    # Every window here is read-write (data/ and the bundle code alike), so writable
    # tracks readable: a path reachable inside a window is writable.
    return _readable(path, mask)


class TestCronAppsMask:
    def test_own_bundle_script_reads_its_bundle_and_no_secret(self, crew_home):
        own = crew_home / "apps" / OWN
        other = crew_home / "apps" / OTHER

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert not mask.refusal
        assert _readable(str(own / "job.py"), mask)
        assert _readable(str(own / "backend" / "server.mjs"), mask)
        assert _readable(str(own / "lib"), mask)
        assert _writable(str(own / "data" / "state.json"), mask), "data/ stays writable"
        assert not _readable(str(own / ".app_secret"), mask)
        assert not _readable(str(other / ".app_secret"), mask)
        assert not _readable(str(other / "backend" / "server.mjs"), mask)
        assert not _readable(str(other / "data"), mask)

    def test_own_secret_is_hidden_and_required(self, crew_home):
        own = crew_home / "apps" / OWN
        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert str(own / ".app_secret") in mask.hidden
        assert str(own / ".app_secret") in mask.required
        assert str(crew_home / "apps" / OTHER / ".app_secret") not in mask.required

    def test_unrelated_cron_sees_no_app(self, crew_home):
        script = crew_home / "crons" / "job.py"
        script.write_text("def run(ctx):\n    pass\n")

        mask = cron_apps_mask(script_file=str(script))

        assert mask.windows == ()
        assert not mask.refusal
        for name in (OWN, OTHER):
            assert not _readable(str(crew_home / "apps" / name / ".app_secret"), mask)
            assert not _readable(str(crew_home / "apps" / name / "job.py"), mask)
        # An app installed after the spawn lands under the masked tree too.
        assert not _readable(str(crew_home / "apps" / "installed-later" / ".app_secret"), mask)

    def test_host_stamped_owner_gets_its_bundle(self, crew_home):
        # A builtin app's script lives in the package, so only ``created_by`` says
        # which installed tree is its own.
        mask = cron_apps_mask(owner_app=OWN)

        assert _readable(str(crew_home / "apps" / OWN / "backend" / "server.mjs"), mask)
        assert _writable(str(crew_home / "apps" / OWN / "data" / "x"), mask)
        assert not _readable(str(crew_home / "apps" / OWN / ".app_secret"), mask)
        assert not _readable(str(crew_home / "apps" / OTHER / "job.py"), mask)

    @pytest.mark.parametrize("owner", ["", "..", "../other-app", ".own-app-secret-tmp", "x/y"])
    def test_an_unsafe_owner_name_opens_nothing(self, crew_home, owner):
        assert cron_apps_mask(owner_app=owner).windows == ()

    def test_command_naming_a_bundle_reads_code_not_data_or_secret(self, crew_home):
        own = crew_home / "apps" / OWN
        mask = cron_apps_mask(command=f"node {own / 'backend' / 'server.mjs'} --flag")

        assert not mask.refusal
        assert _readable(str(own / "backend" / "server.mjs"), mask)
        assert not _readable(str(own / "data"), mask)
        assert not _readable(str(own / ".app_secret"), mask)
        assert not _readable(str(crew_home / "apps" / OTHER / "job.py"), mask)

    def test_command_naming_a_secret_gets_no_secret(self, crew_home):
        target = crew_home / "apps" / OTHER / ".app_secret"
        mask = cron_apps_mask(command=f"cat {target}")

        assert not _readable(str(target), mask)

    def test_absent_apps_tree_is_created_so_the_mask_has_a_target(self, tmp_path, monkeypatch):
        crew = tmp_path / "home" / ".kiro" / "crew"
        crew.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("KIROCREW_HOME", str(crew))

        mask = cron_apps_mask()

        assert (crew / "apps").is_dir()
        assert str(crew / "apps") in mask.hidden

    def test_both_spellings_of_a_symlinked_home_are_masked(self, linked_crew_home):
        real, link = linked_crew_home

        mask = cron_apps_mask(script_file=str(real / "apps" / OWN / "job.py"))

        for root in (real, link):
            assert str(root / "apps") in mask.hidden, f"{root} spelling of apps/ is not masked"
            assert not _readable(str(root / "apps" / OTHER / ".app_secret"), mask)
            assert not _readable(str(root / "apps" / OWN / ".app_secret"), mask)
            assert _readable(str(root / "apps" / OWN / "backend" / "server.mjs"), mask)
            assert _writable(str(root / "apps" / OWN / "data" / "x"), mask)


class TestPerWindowScan:
    def test_own_bundle_linking_another_secret_refuses_only_that_app(self, crew_home):
        own = crew_home / "apps" / OWN
        os.link(crew_home / "apps" / OTHER / ".app_secret", own / "data" / "stolen")

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert mask.refusal, "the owning app's cron must be refused"
        assert OWN in mask.refusal and "stolen" in mask.refusal
        assert mask.windows == ()
        # The apps tree is still masked in the refusal result, so nothing leaks.
        assert str(crew_home / "apps") in mask.hidden

    def test_a_stray_link_in_one_bundle_does_not_refuse_another_apps_cron(self, crew_home):
        # OTHER's bundle holds a bad link, but OWN's cron (its own clean bundle) runs.
        other = crew_home / "apps" / OTHER
        os.link(crew_home / "apps" / OWN / ".app_secret", other / "data" / "stolen")

        mask = cron_apps_mask(script_file=str(crew_home / "apps" / OWN / "job.py"))

        assert not mask.refusal
        assert mask.windows, "a clean bundle must still get its window"
        assert _readable(str(crew_home / "apps" / OWN / "job.py"), mask)

    def test_command_naming_a_linking_bundle_withholds_only_that_window(self, crew_home):
        other = crew_home / "apps" / OTHER
        os.link(crew_home / "apps" / OWN / ".app_secret", other / "data" / "stolen")

        # The command names OTHER's bundle; its window is withheld, the cron still runs.
        mask = cron_apps_mask(command=f"node {other / 'backend' / 'server.mjs'}")

        assert not mask.refusal
        assert not _readable(str(other / "backend" / "server.mjs"), mask)
        assert not _readable(str(other / "data" / "stolen"), mask)

    def test_a_second_link_to_the_bundles_own_secret_also_refuses(self, crew_home):
        # The own secret is masked at its own name, but a SECOND link to it elsewhere in
        # the bundle is not at that name, so it would ride into the window. The
        # per-window scan catches it and refuses the owning app's cron.
        own = crew_home / "apps" / OWN
        os.link(own / ".app_secret", own / "lib" / "copy")

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert mask.refusal and "copy" in mask.refusal
        assert mask.windows == ()


class TestSettledBundlesOnly:
    @pytest.mark.parametrize("staging", ["data-tmp", "secret-tmp"])
    def test_a_bundle_mid_update_stays_masked(self, crew_home, staging):
        (crew_home / "apps" / f".{OWN}-{staging}").mkdir()
        own = crew_home / "apps" / OWN

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert mask.windows == ()
        assert not mask.refusal
        assert not _readable(str(own / "job.py"), mask)

    def test_each_window_is_pinned_to_the_planned_directory(self, crew_home):
        own = crew_home / "apps" / OWN
        mask = cron_apps_mask(script_file=str(own / "job.py"))

        pins = {path: (dev, ino) for path, dev, ino in mask.window_ids}
        assert mask.windows
        for window in mask.windows:
            info = os.lstat(window)
            assert pins[window] == (info.st_dev, info.st_ino)

    def test_a_bundle_without_a_secret_stays_masked(self, crew_home):
        own = crew_home / "apps" / OWN
        (own / ".app_secret").unlink()

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert mask.windows == ()
        assert not mask.refusal

    def test_a_secret_linked_outside_the_apps_tree_is_the_known_residual(self, crew_home, tmp_path):
        # A hard link to the secret at a path OUTSIDE the apps tree is a known residual:
        # the per-window scan walks only the bundle tree, so it does not see it, the
        # bundle is still settled, and its window opens with the secret masked by name.
        # The external link stays readable -- the gap the design keeps.
        own = crew_home / "apps" / OWN
        os.link(own / ".app_secret", tmp_path / "elsewhere")

        mask = cron_apps_mask(script_file=str(own / "job.py"))

        assert not mask.refusal
        assert mask.windows, "an external link is not an in-bundle link; the window opens"
        assert not _readable(str(own / ".app_secret"), mask)

    def test_a_symlinked_secret_keeps_the_bundle_masked(self, crew_home, tmp_path):
        # A symlinked secret points at bytes no window pins, so the bundle is not settled.
        own = crew_home / "apps" / OWN
        (own / ".app_secret").unlink()
        (tmp_path / "real-secret").write_text("x\n")
        (own / ".app_secret").symlink_to(tmp_path / "real-secret")

        assert cron_apps_mask(script_file=str(own / "job.py")).windows == ()


class TestMaskHelperEdges:
    def test_a_script_outside_the_apps_tree_names_no_bundle(self, crew_home):
        real = os.path.realpath(crew_home / "apps")
        assert cron_script._app_name_under(str(crew_home / "crons" / "x.py"), real) == ""
        assert cron_script._app_name_under(real, real) == ""

    def test_an_unparsable_command_names_no_bundle(self, crew_home):
        real = os.path.realpath(crew_home / "apps")
        assert cron_script._command_bundle_refs("echo 'unterminated", real) == []

    def test_a_relative_command_path_names_no_bundle(self, crew_home):
        real = os.path.realpath(crew_home / "apps")
        assert cron_script._command_bundle_refs(f"node apps/{OWN}/job.py", real) == []

    def test_a_linked_bundle_is_not_a_bundle(self, crew_home, tmp_path):
        target = tmp_path / "outside-bundle"
        target.mkdir()
        (crew_home / "apps" / "linked-app").symlink_to(target, target_is_directory=True)

        assert cron_apps_mask(owner_app="linked-app").windows == ()

    def test_a_variable_spelled_command_path_names_the_bundle(self, crew_home, monkeypatch):
        own = crew_home / "apps" / OWN
        monkeypatch.setenv("APPS_ROOT_FOR_TEST", str(crew_home / "apps"))

        mask = cron_apps_mask(command=f"node $APPS_ROOT_FOR_TEST/{OWN}/backend/server.mjs")

        assert _readable(str(own / "backend" / "server.mjs"), mask)
        assert not _readable(str(own / ".app_secret"), mask)

    def test_a_failed_mkdir_still_masks_the_tree(self, crew_home, monkeypatch):
        real_mkdir = Path.mkdir

        def _refuse(self, *args, **kwargs):
            if self.name == "apps":
                raise PermissionError("read-only home")
            return real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", _refuse)

        mask = cron_apps_mask()

        assert str(crew_home / "apps") in mask.hidden


class TestSeatbeltProfile:
    """The macOS Seatbelt profile the mask produces: bundle readable, secret denied.

    Pinned as profile TEXT (no backend needed), so the macOS regression -- a bundle
    window refused because it holds the masked ``.app_secret`` -- cannot come back
    without this failing. The window is read-ONLY (carved out of the read deny), the
    secret keeps its own literal deny (deny-wins inside the window), and the bundle code
    is not writable.
    """

    def test_bundle_reads_secret_denied_and_bundle_write_sealed(self, crew_home):
        from kiro_crew import sandbox

        own = crew_home / "apps" / OWN
        apps = str(crew_home / "apps")
        mask = cron_apps_mask(script_file=str(own / "job.py"))
        assert not mask.refusal and str(own) in mask.readonly

        profile = sandbox._build_seatbelt_profile(
            "cc",
            extra_hidden_dirs=mask.hidden,
            extra_private_dirs=mask.windows,
            extra_readonly_private_dirs=mask.readonly,
        )
        lines = profile.splitlines()
        sub = f"(subpath {json.dumps(apps)})"
        read = [ln for ln in lines if ln.startswith(f"(deny file-read* (require-all {sub}")]
        write = [ln for ln in lines if ln.startswith(f"(deny file-write* (require-all {sub}")]
        write_blanket = [ln for ln in lines if ln == f"(deny file-write* {sub})"]

        # The bundle is carved out of the apps-tree READ deny (code imports).
        assert read and f"(require-not (subpath {json.dumps(str(own))}))" in read[0]
        # The bundle is NOT carved out of the WRITE deny (read-only): either a blanket
        # write deny over apps/, or a write deny whose exceptions do not include the
        # read-only bundle.
        assert write_blanket or (
            write and f"(require-not (subpath {json.dumps(str(own))}))" not in write[0]
        )
        # The owned bundle's data/ IS writable (a nested read-write window).
        data_exc = f"(require-not (subpath {json.dumps(str(own / 'data'))}))"
        assert any(data_exc in ln for ln in write) or write_blanket
        # The secret keeps its own read deny, which deny-wins inside the window.
        secret = json.dumps(str(own / ".app_secret"))
        assert f"(deny file-read* (literal {secret}))" in lines


# --------------------------------------------------------------------------- #
# REAL sandbox cases a-f: actually launch a child under the real backend.
# --------------------------------------------------------------------------- #


def _backend() -> str:
    from kiro_crew import sandbox

    return sandbox.detect_backend(config_mode="cc")


_REAL_BACKEND = pytest.mark.skipif(
    _backend() not in ("namespace", "sandbox-exec"),
    reason=(
        f"no real OS sandbox backend here (detect_backend='{_backend()}'): a nested "
        "agent sandbox or a host/CI runner without unprivileged user namespaces. The "
        "macOS CI job runs these under real Seatbelt; the Linux namespace runner runs "
        "them where userns is permitted."
    ),
)


def _run_child(
    mask: CronAppsMask, program: str, *, mode: str = "cc"
) -> subprocess.CompletedProcess:
    """Launch a python child under the REAL sandbox with *mask* applied, running *program*.

    Mirrors what ``run_script_sandboxed`` / ``run_command_sandboxed`` build: the apps
    tree masked, the owning bundle a window, the secret masked and required inside it.
    """
    from kiro_crew import sandbox

    argv = [sys.executable, "-c", program]
    wrapped, cleanup = sandbox.wrap_argv(
        argv,
        mode=mode,
        extra_hidden_dirs=mask.hidden,
        extra_private_dirs=mask.windows,
        extra_private_dir_ids=mask.window_ids,
        extra_required_mask_targets=mask.required,
    )
    try:
        return subprocess.run(
            wrapped,
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "TMPDIR": "/tmp"},
        )
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass


def _reads(path: Path) -> str:
    # A tiny program that prints OK if it can read *path*, else DENIED.
    return textwrap.dedent(f"""
        import sys
        try:
            with open({str(path)!r}) as fh:
                fh.read()
            sys.stdout.write("OK")
        except OSError:
            sys.stdout.write("DENIED")
        """)


@_REAL_BACKEND
class TestRealSandbox:
    """Cases a-f from the re-land plan, run under the real OS sandbox backend."""

    def test_a_app_cron_imports_a_sibling_in_its_own_bundle(self, crew_home):
        own = crew_home / "apps" / OWN
        mask = cron_apps_mask(script_file=str(own / "job.py"))
        prog = textwrap.dedent(f"""
            import sys, os
            # The bundle's own code is readable: list it and read a sibling module.
            names = sorted(os.listdir({str(own / "backend")!r}))
            with open({str(own / "backend" / "sibling.mjs")!r}) as fh:
                fh.read()
            sys.stdout.write("OK:" + ",".join(names))
            """)
        result = _run_child(mask, prog)
        assert result.returncode == 0, result.stderr
        assert result.stdout.startswith("OK:"), result.stdout

    def test_b_script_and_command_cron_cannot_read_another_apps_secret(self, crew_home):
        other_secret = crew_home / "apps" / OTHER / ".app_secret"
        # Script cron: owns OWN's bundle, reads OTHER's secret -> denied.
        smask = cron_apps_mask(script_file=str(crew_home / "apps" / OWN / "job.py"))
        sres = _run_child(smask, _reads(other_secret))
        assert sres.returncode == 0, sres.stderr
        assert sres.stdout == "DENIED", f"script cron read another app's secret: {sres.stdout}"
        # Command cron: no window, reads OTHER's secret -> denied.
        cmask = cron_apps_mask(command="true")
        cres = _run_child(cmask, _reads(other_secret))
        assert cres.returncode == 0, cres.stderr
        assert cres.stdout == "DENIED", f"command cron read another app's secret: {cres.stdout}"

    def test_c_app_cron_cannot_read_its_own_secret(self, crew_home):
        own = crew_home / "apps" / OWN
        mask = cron_apps_mask(script_file=str(own / "job.py"))
        result = _run_child(mask, _reads(own / ".app_secret"))
        assert result.returncode == 0, result.stderr
        assert result.stdout == "DENIED", f"app cron read its OWN secret: {result.stdout}"

    def test_d_symlinked_home_covers_both_spellings(self, linked_crew_home):
        real, link = linked_crew_home
        mask = cron_apps_mask(script_file=str(real / "apps" / OWN / "job.py"))
        for root in (real, link):
            result = _run_child(mask, _reads(root / "apps" / OTHER / ".app_secret"))
            assert result.returncode == 0, result.stderr
            assert result.stdout == "DENIED", f"{root} spelling leaked a secret: {result.stdout}"

    def test_e_hardlink_in_one_bundle_refuses_only_that_app(self, crew_home):
        # X=OWN holds a link to Y=OTHER's secret inside its bundle. OWN's cron is refused
        # at the planner (never spawned); OTHER's cron (clean bundle) still runs and masks.
        own = crew_home / "apps" / OWN
        os.link(crew_home / "apps" / OTHER / ".app_secret", own / "lib" / "stolen")

        xmask = cron_apps_mask(script_file=str(own / "job.py"))
        assert xmask.refusal and OWN in xmask.refusal, "X's cron must be refused"

        ymask = cron_apps_mask(script_file=str(crew_home / "apps" / OTHER / "job.py"))
        assert not ymask.refusal, "Y's cron must still run"
        yres = _run_child(ymask, _reads(crew_home / "apps" / OWN / ".app_secret"))
        assert yres.returncode == 0, yres.stderr
        assert yres.stdout == "DENIED", "Y's cron could read another app's secret"

    def test_f_unrelated_crons_script_sees_no_app_tree(self, crew_home):
        script = crew_home / "crons" / "job.py"
        script.write_text("def run(ctx):\n    pass\n")
        mask = cron_apps_mask(script_file=str(script))
        assert mask.windows == ()
        # Reads of either app's secret and bundle code are denied; the tree is masked.
        for name in (OWN, OTHER):
            res = _run_child(mask, _reads(crew_home / "apps" / name / ".app_secret"))
            assert res.returncode == 0, res.stderr
            assert res.stdout == "DENIED", f"unrelated cron read {name}'s secret: {res.stdout}"
