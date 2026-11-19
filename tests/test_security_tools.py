"""The scripts under tools/security/ that the security workflow runs: the Grype gate (which proves
its settings can fail before trusting a clean scan), the action pin check and the image probe. The
workflow's steps are pinned by tests/test_security_checks.py; these hold each script's behaviour."""

import importlib.util
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest
from django.conf import settings

ROOT = Path(settings.BASE_DIR)


def load(name):
    spec = importlib.util.spec_from_file_location(
        f"tools_{name}", ROOT / f"tools/security/{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load("grype_gate")
action_pins = load("check_action_pins")
image_contents = load("image_contents")

# ------------------------------------------------------------------------- grype_gate


def report(*names):
    return {"matches": [{"artifact": {"name": name}} for name in names]}


class FakeGrype:
    """A runner that records each command and answers the canary and the real scan."""

    def __init__(self, canary_code, canary_report, scan_code=0):
        self.canary_code, self.canary_report, self.scan_code = canary_code, canary_report, scan_code
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        output = command[command.index("--output") + 1]
        if output.startswith("json="):
            if self.canary_report is not None:
                Path(output.removeprefix("json=")).write_text(json.dumps(self.canary_report))
            return subprocess.CompletedProcess(command, self.canary_code, "out", "err")
        return subprocess.CompletedProcess(command, self.scan_code, "", "")


def run_gate(name, runner, sbom=Path("/work/deps.cdx.json")):
    return gate.run(name, "/bin/grype", sbom, runner=runner)


@pytest.mark.parametrize("name", sorted(gate.GATES))
@pytest.mark.parametrize("scan_code", [0, 2, 1])
def test_a_sound_canary_returns_the_real_scans_exit_code(name, scan_code, capsys):
    runner = FakeGrype(2, report(*gate.GATES[name]["canary"], "extra"), scan_code)
    assert run_gate(name, runner) == scan_code
    assert len(runner.calls) == 2
    assert "Canary:" in capsys.readouterr().out


@pytest.mark.parametrize("name", sorted(gate.GATES))
def test_a_canary_that_exits_0_stops_the_gate_without_the_real_scan(name, capsys):
    runner = FakeGrype(0, report(*gate.GATES[name]["canary"]))
    assert run_gate(name, runner) == 1
    assert len(runner.calls) == 1, "the real scan must not run"
    assert "cannot be trusted" in capsys.readouterr().out


def test_a_canary_that_errors_stops_the_gate():
    runner = FakeGrype(1, report(*gate.GATES["image"]["canary"]))
    assert run_gate("image", runner) == 1 and len(runner.calls) == 1


@pytest.mark.parametrize("missing", sorted(gate.GATES["dependencies"]["canary"]))
def test_a_canary_missing_a_package_stops_the_dependency_gate(missing, capsys):
    names = gate.GATES["dependencies"]["canary"] - {missing}
    runner = FakeGrype(2, report(*names))
    assert run_gate("dependencies", runner) == 1
    assert len(runner.calls) == 1
    assert missing in capsys.readouterr().out


def test_a_canary_that_writes_no_report_stops_the_gate():
    runner = FakeGrype(2, None)
    assert run_gate("image", runner) == 1 and len(runner.calls) == 1


def test_the_real_scan_uses_the_canarys_arguments_and_config():
    runner = FakeGrype(2, report("django", "ecdsa", "bootstrap", "actions/download-artifact"))
    sbom = Path("/work/deps.cdx.json")
    assert run_gate("dependencies", runner, sbom) == 0
    (canary, _), (real, _) = runner.calls
    assert canary[0] == real[0] == "/bin/grype"
    assert canary[1] == f"sbom:{gate.CANARY}" and real[1] == f"sbom:{sbom}"
    config = ["--config", str(ROOT / ".grype.yaml")]
    flags = gate.GATES["dependencies"]["args"]
    assert canary[2:4] == real[2:4] == config
    assert canary[4:6] == real[4:6] == flags
    assert canary[6:] == ["--output", canary[7]] and canary[7].startswith("json=")
    assert real[6:] == ["--output", "table"]


def test_the_canary_runs_in_a_scratch_directory_and_captures_its_output():
    runner = FakeGrype(2, report(*gate.GATES["image"]["canary"]))
    run_gate("image", runner)
    kwargs = runner.calls[0][1]
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    assert Path(kwargs["cwd"]).resolve() != ROOT.resolve()


def test_the_gate_settings_and_canary_file():
    assert gate.CONFIG.resolve() == (ROOT / ".grype.yaml").resolve()
    components = json.loads(gate.CANARY.read_text())["components"]
    listed = {component["name"] for component in components}
    assert set().union(*(g["canary"] for g in gate.GATES.values())) <= listed


def test_canary_problems_names_each_reason():
    assert gate.canary_problems("image", 2, report("django")) == []
    assert len(gate.canary_problems("image", 0, report("django"))) == 1
    assert len(gate.canary_problems("image", 1, {})) == 2
    text = gate.canary_problems("dependencies", 2, report("django"))[0]
    assert "actions/download-artifact" in text and "ecdsa" in text


def test_the_gate_command_passes_the_sbom_config_and_levels():
    command = gate.grype_command("g", "image", Path("s.json"), ["--output", "table"])
    assert command == [
        "g",
        "sbom:s.json",
        "--config",
        str(gate.CONFIG),
        "--only-fixed",
        "--fail-on",
        "high",
        "--output",
        "table",
    ]


def test_main_runs_the_named_gate_on_the_resolved_sbom(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(gate, "run", lambda *args: seen.append(args) or 7)
    (tmp_path / "s.json").write_text("{}")
    monkeypatch.chdir(tmp_path)
    assert gate.main(["image", "--grype", "/bin/grype", "s.json"]) == 7
    assert seen == [("image", "/bin/grype", (tmp_path / "s.json").resolve())]
    with pytest.raises(SystemExit):
        gate.main(["unknown", "--grype", "g", "s.json"])


# ------------------------------------------------------------------ check_action_pins

SHA_A = "6b208ae046db98c579e8a3aa621ab581ff575935"
SHA_B = "fa0a91b85d4f404e444e00e005971372dc801d16"
WORKFLOW = f"""\
jobs:
  a:
    steps:
      - uses: actions/checkout@{SHA_A} # v4.1.1
        with:
          persist-credentials: false
      - uses: github/codeql-action/init@{SHA_B} # v4.39.3
      - uses: ./.github/actions/local
      - uses: actions/cache@v4
      - name: Named
        uses: actions/upload-artifact@{SHA_B} # v7
      - uses: actions/setup-node@{SHA_A}
      - run: echo uses
"""


def test_pins_parses_commit_pins_with_version_comments():
    found, unreadable = action_pins.pins(WORKFLOW, "w.yml")
    assert found == [
        ("w.yml:4", "actions/checkout", SHA_A, "v4.1.1"),
        ("w.yml:7", "github/codeql-action", SHA_B, "v4.39.3"),
        ("w.yml:11", "actions/upload-artifact", SHA_B, "v7"),
    ]
    assert unreadable == [
        "w.yml:9: not a commit pin with a version comment",
        "w.yml:12: not a commit pin with a version comment",
    ]


def test_pins_skips_local_actions():
    assert action_pins.pins("      - uses: ./local\n") == ([], [])


def test_pins_reads_the_repositorys_workflows_without_unreadable_lines():
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        found, unreadable = action_pins.pins(path.read_text(), path.name)
        assert found and not unreadable, (path.name, unreadable)


def fake_fetch(objects):
    calls = []

    def fetch(path):
        calls.append(path)
        return objects[path]

    fetch.calls = calls
    return fetch


def test_tag_commit_returns_a_lightweight_tags_commit():
    fetch = fake_fetch({"/repos/o/r/git/ref/tags/v1": {"object": {"type": "commit", "sha": SHA_A}}})
    assert action_pins.tag_commit("o/r", "v1", fetch) == SHA_A


def test_tag_commit_follows_annotated_tags():
    fetch = fake_fetch(
        {
            "/repos/o/r/git/ref/tags/v1": {"object": {"type": "tag", "sha": "t1"}},
            "/repos/o/r/git/tags/t1": {"object": {"type": "tag", "sha": "t2"}},
            "/repos/o/r/git/tags/t2": {"object": {"type": "commit", "sha": SHA_B}},
        }
    )
    assert action_pins.tag_commit("o/r", "v1", fetch) == SHA_B
    assert len(fetch.calls) == 3


def test_tag_commit_gives_up_on_endless_nesting():
    objects = {"/repos/o/r/git/ref/tags/v1": {"object": {"type": "tag", "sha": "t"}}}
    objects["/repos/o/r/git/tags/t"] = {"object": {"type": "tag", "sha": "t"}}
    with pytest.raises(ValueError, match="nested"):
        action_pins.tag_commit("o/r", "v1", fake_fetch(objects))


def test_problems_is_empty_when_every_comment_names_the_pinned_commit():
    pinned = [("w:1", "a/b", SHA_A, "v1"), ("w:2", "c/d", SHA_B, "v2")]
    resolve = {("a/b", "v1"): SHA_A, ("c/d", "v2"): SHA_B}.get
    assert action_pins.problems(pinned, lambda repo, tag: resolve((repo, tag))) == []


def test_problems_reports_a_comment_that_names_another_release():
    resolver = {
        ("actions/download-artifact", "v4.1.1"): SHA_A,
        ("actions/download-artifact", "v4.1.8"): SHA_B,
    }
    lines = action_pins.problems(
        [("w:3", "actions/download-artifact", SHA_A, "v4.1.8")],
        lambda repo, tag: resolver[(repo, tag)],
    )
    assert len(lines) == 1
    assert "w:3" in lines[0] and "v4.1.8" in lines[0] and SHA_B in lines[0]


@pytest.mark.parametrize(
    "error", [urllib.error.URLError("offline"), KeyError("object"), ValueError("x")]
)
def test_problems_reports_a_tag_that_cannot_be_resolved(error):
    def resolve(repo, tag):
        raise error

    lines = action_pins.problems([("w:1", "a/b", SHA_A, "v9")], resolve)
    assert len(lines) == 1 and "unresolved" in lines[0] and "v9" in lines[0]


def test_problems_looks_each_tag_up_once():
    calls = []

    def resolve(repo, tag):
        calls.append((repo, tag))
        return SHA_A

    pinned = [
        ("w:1", "a/b", SHA_A, "v1"),
        ("x:9", "a/b", SHA_A, "v1"),
        ("x:10", "a/b", SHA_B, "v1"),
    ]
    lines = action_pins.problems(pinned, resolve)
    assert calls == [("a/b", "v1")]
    assert len(lines) == 1 and lines[0].startswith("x:10")


# ------------------------------------------------------------------------ image_contents

OS_RELEASE = (
    'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\n'
    'NAME="Debian GNU/Linux"\n'
    'VERSION_ID="13"\n'
    "ID=debian\n"
)
DPKG_STATUS = """\
Package: libc6
Status: install ok installed
Version: 2.41-12
Description: GNU C Library
 continuation line
 Package: not-a-package
 Version: 9.9

Package: libssl3
Status: install ok installed
Version: 1:3.5.1-1

Package: removed-tool
Status: deinstall ok config-files
Version: 1.0

Package: half-installed
Status: install ok half-installed
Version: 2.0

Package: libc6
Status: install ok installed
Version: 2.41-13

Package: no-status
Version: 3.0
"""


def test_os_release_reads_the_id_and_version(tmp_path):
    path = tmp_path / "os-release"
    path.write_text(OS_RELEASE)
    assert image_contents.os_release(path) == {"id": "debian", "version_id": "13"}


def test_os_release_without_the_fields_gives_empty_strings(tmp_path):
    path = tmp_path / "os-release"
    path.write_text("NAME=x\n")
    assert image_contents.os_release(path) == {"id": "", "version_id": ""}


def test_deb_packages_lists_only_installed_packages_with_every_version(tmp_path):
    path = tmp_path / "status"
    path.write_text(DPKG_STATUS)
    assert image_contents.deb_packages(path) == {
        "libc6": ["2.41-12", "2.41-13"],
        "libssl3": ["1:3.5.1-1"],
    }


def test_deb_packages_ignores_continuation_lines(tmp_path):
    path = tmp_path / "status"
    path.write_text(DPKG_STATUS)
    assert "not-a-package" not in image_contents.deb_packages(path)


def test_python_packages_lists_this_environment():
    found = image_contents.python_packages()
    assert "Django" in found or "django" in {name.lower() for name in found}
    assert all(isinstance(versions, list) and versions for versions in found.values())


def test_the_probe_prints_json_in_the_shape_the_coverage_check_reads(monkeypatch, capsys, tmp_path):
    (tmp_path / "os-release").write_text(OS_RELEASE)
    (tmp_path / "status").write_text(DPKG_STATUS)
    original = image_contents.os_release, image_contents.deb_packages
    monkeypatch.setattr(image_contents, "os_release", lambda: original[0](tmp_path / "os-release"))
    monkeypatch.setattr(image_contents, "deb_packages", lambda: original[1](tmp_path / "status"))
    image_contents.main()
    contents = json.loads(capsys.readouterr().out)
    assert set(contents) == {"os", "python", "deb", "pypi"}
    assert contents["os"]["id"] == "debian" and contents["deb"]["libssl3"] == ["1:3.5.1-1"]
    coverage = load("sbom_coverage")
    problems, _ = coverage.check_image(
        {"components": []}, contents, {"project": {"dependencies": ["pytest"]}}, {"packages": {}}
    )
    assert not any("incomplete" in problem for problem in problems)
