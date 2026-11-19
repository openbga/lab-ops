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
        # The SBOM each scan was given, read while the gate's scratch directory still exists.
        self.scanned = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        self.scanned.append(json.loads(Path(command[1].removeprefix("sbom:")).read_text()))
        output = command[command.index("--output") + 1]
        if output.startswith("json="):
            if self.canary_report is not None:
                Path(output.removeprefix("json=")).write_text(json.dumps(self.canary_report))
            return subprocess.CompletedProcess(command, self.canary_code, "out", "err")
        return subprocess.CompletedProcess(command, self.scan_code, "", "")


def sbom_file(tmp_path, *components):
    path = tmp_path / "deps.cdx.json"
    path.write_text(json.dumps({"bomFormat": "CycloneDX", "components": list(components)}))
    return path


def run_gate(name, runner, sbom):
    return gate.run(name, "/bin/grype", sbom, runner=runner)


@pytest.mark.parametrize("name", sorted(gate.GATES))
@pytest.mark.parametrize("scan_code", [0, 2, 1])
def test_a_sound_canary_returns_the_real_scans_exit_code(name, scan_code, capsys, tmp_path):
    runner = FakeGrype(2, report(*gate.GATES[name]["canary"], "extra"), scan_code)
    assert run_gate(name, runner, sbom_file(tmp_path)) == scan_code
    assert len(runner.calls) == 2
    assert "Canary:" in capsys.readouterr().out


@pytest.mark.parametrize("name", sorted(gate.GATES))
def test_a_canary_that_exits_0_stops_the_gate_without_the_real_scan(name, capsys, tmp_path):
    runner = FakeGrype(0, report(*gate.GATES[name]["canary"]))
    assert run_gate(name, runner, sbom_file(tmp_path)) == 1
    assert len(runner.calls) == 1, "the real scan must not run"
    assert "cannot be trusted" in capsys.readouterr().out


def test_a_canary_that_errors_stops_the_gate(tmp_path):
    runner = FakeGrype(1, report(*gate.GATES["image"]["canary"]))
    assert run_gate("image", runner, sbom_file(tmp_path)) == 1 and len(runner.calls) == 1


@pytest.mark.parametrize("missing", sorted(gate.GATES["dependencies"]["canary"]))
def test_a_canary_missing_a_package_stops_the_dependency_gate(missing, capsys, tmp_path):
    names = gate.GATES["dependencies"]["canary"] - {missing}
    runner = FakeGrype(2, report(*names))
    assert run_gate("dependencies", runner, sbom_file(tmp_path)) == 1
    assert len(runner.calls) == 1
    assert missing in capsys.readouterr().out


def test_a_canary_that_writes_no_report_stops_the_gate(tmp_path):
    runner = FakeGrype(2, None)
    assert run_gate("image", runner, sbom_file(tmp_path)) == 1 and len(runner.calls) == 1


def test_the_real_scan_uses_the_canarys_arguments_and_config(tmp_path):
    runner = FakeGrype(2, report(*gate.GATES["dependencies"]["canary"]))
    sbom = sbom_file(tmp_path)
    assert run_gate("dependencies", runner, sbom) == 0
    (canary, _), (real, _) = runner.calls
    assert canary[0] == real[0] == "/bin/grype"
    # Each scan reads a prepared copy in the gate's scratch directory, never the file itself.
    assert canary[1].startswith("sbom:") and canary[1].endswith("/canary.cdx.json")
    assert real[1].startswith("sbom:") and real[1].endswith("/scan.cdx.json")
    assert real[1] != f"sbom:{sbom}" and canary[1] != f"sbom:{gate.CANARY}"
    config = ["--config", str(ROOT / ".grype.yaml")]
    flags = gate.GATES["dependencies"]["args"]
    assert canary[2:4] == real[2:4] == config
    assert canary[4:6] == real[4:6] == flags
    assert canary[6:] == ["--output", canary[7]] and canary[7].startswith("json=")
    assert real[6:] == ["--output", "table"]


def test_the_canary_runs_in_a_scratch_directory_and_captures_its_output(tmp_path):
    runner = FakeGrype(2, report(*gate.GATES["image"]["canary"]))
    run_gate("image", runner, sbom_file(tmp_path))
    kwargs = runner.calls[0][1]
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    assert Path(kwargs["cwd"]).resolve() != ROOT.resolve()


def test_the_gate_settings_and_canary_file():
    assert gate.CONFIG.resolve() == (ROOT / ".grype.yaml").resolve()
    components = json.loads(gate.CANARY.read_text())["components"]
    listed = {component["name"] for component in components}
    # The canary lists a sub-path action (github/codeql-action/init); the gate adds the
    # repository's own name, which is the one advisories use.
    assert "github/codeql-action/init" in listed and "github/codeql-action" not in listed
    prepared = {
        c["name"] for c in gate.with_repository_actions({"components": components})["components"]
    }
    assert set().union(*(g["canary"] for g in gate.GATES.values())) <= prepared


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


ACTION = {"name": "syft:package:type", "value": "github-action"}


def action_component(name, version, ref=None):
    return {
        "bom-ref": ref or name,
        "type": "library",
        "name": name,
        "version": version,
        "purl": f"pkg:github/{'/'.join(name.split('/')[:2])}@{version}",
        "properties": [ACTION],
    }


def test_with_repository_actions_adds_each_repository_once_per_version():
    original = {
        "bomFormat": "CycloneDX",
        "components": [
            action_component("github/codeql-action/init", "v3.28.0"),
            action_component("github/codeql-action/analyze", "v3.28.0"),
            action_component("github/codeql-action/analyze", "v4.0.0", ref="analyze-v4"),
        ],
    }
    before = json.dumps(original)
    prepared = gate.with_repository_actions(original)
    assert json.dumps(original) == before, "the SBOM given must not change"
    assert prepared["bomFormat"] == "CycloneDX"
    added = prepared["components"][3:]
    assert prepared["components"][:3] == original["components"]
    assert [(c["name"], c["version"]) for c in added] == [
        ("github/codeql-action", "v3.28.0"),
        ("github/codeql-action", "v4.0.0"),
    ]
    assert added[0]["purl"] == "pkg:github/github/codeql-action@v3.28.0"
    assert ACTION in added[0]["properties"] and added[0]["type"] == "library"
    assert len({c["bom-ref"] for c in prepared["components"]}) == 5


def test_with_repository_actions_leaves_top_level_actions_and_non_actions_alone():
    python_package = {
        "name": "a/b/c",
        "version": "1",
        "purl": "pkg:pypi/c@1",
        "properties": [{"name": "syft:package:type", "value": "python"}],
    }
    components = [
        action_component("actions/checkout", "v7.0.1"),
        action_component("github/codeql-action", "v4.0.0"),
        action_component("github/codeql-action/init", "v4.0.0"),  # its repository is listed
        python_package,
        {"name": "x/y/z", "version": "1"},
    ]
    prepared = gate.with_repository_actions({"components": components})
    assert prepared["components"] == components
    assert gate.with_repository_actions({})["components"] == []


def test_prepare_writes_the_prepared_copy_and_leaves_the_original(tmp_path):
    source = sbom_file(tmp_path, action_component("github/codeql-action/init", "v3.28.0"))
    before = source.read_text()
    target = gate.prepare(source, tmp_path, "scan.cdx.json")
    assert target == tmp_path / "scan.cdx.json" and source.read_text() == before
    names = [c["name"] for c in json.loads(target.read_text())["components"]]
    assert names == ["github/codeql-action/init", "github/codeql-action"]


@pytest.mark.parametrize("name", sorted(gate.GATES))
def test_the_gate_scans_the_prepared_copy_of_the_canary_and_of_the_sbom(name, tmp_path):
    runner = FakeGrype(2, report(*gate.GATES[name]["canary"]))
    sbom = sbom_file(tmp_path, action_component("github/codeql-action/analyze", "v4.39.3"))
    before = sbom.read_text()
    assert run_gate(name, runner, sbom) == 0
    canary, real = runner.scanned
    names = {c["name"] for c in canary["components"]}
    assert "github/codeql-action" in names and "github/codeql-action/init" in names
    assert [c["name"] for c in real["components"]] == [
        "github/codeql-action/analyze",
        "github/codeql-action",
    ]
    assert sbom.read_text() == before, "the SBOM file given is not rewritten"


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
        ("w.yml:7", "github/codeql-action/init", SHA_B, "v4.39.3"),
        ("w.yml:11", "actions/upload-artifact", SHA_B, "v7"),
    ]
    assert unreadable == [
        f"w.yml:9: {action_pins.READ_AS}",
        f"w.yml:12: {action_pins.READ_AS}",
    ]


def test_pins_skips_local_actions():
    assert action_pins.pins("      - uses: ./local\n") == ([], [])


def test_pins_reads_the_repositorys_workflows_without_unreadable_lines():
    paths = action_pins.workflow_files(ROOT)
    assert {path.name for path in paths} >= {"security.yml", "secrets.yml", "codeql.yml"}
    for path in paths:
        found, unreadable = action_pins.pins(path.read_text(), path.name)
        assert found and not unreadable, (path.name, unreadable)


@pytest.mark.parametrize(
    "lines",
    [
        ["- uses:", "    actions/checkout@v7"],
        ["- uses:", f"    actions/checkout@{SHA_A} # v7"],
        ["- {uses: actions/checkout@v7}"],
        [f"- {{name: x, uses: actions/checkout@{SHA_A} # v7}}"],
        ["- 'uses': actions/checkout@v7"],
        [f"- 'uses': actions/checkout@{SHA_A} # v7"],
        ['- "uses": actions/checkout@v7'],
        [f"- uses: actions/checkout@{SHA_A} # not a version"],
        [f"- uses: actions/checkout@{SHA_A}"],
        ["- uses: actions/checkout@v7 # v7"],
        ["- uses: actions/checkout@main"],
        [f"- uses: 'actions/checkout@{SHA_A}' # v7"],
        ["uses: actions/checkout@v7"],
    ],
    ids=lambda lines: " | ".join(lines),
)
def test_pins_fails_closed_on_every_layout_it_cannot_read_as_one_pinned_line(lines):
    text = "jobs:\n  a:\n    steps:\n" + "".join(f"      {line}\n" for line in lines)
    found, unreadable = action_pins.pins(text, "w.yml")
    assert found == [] and len(unreadable) == 1, (found, unreadable)
    assert unreadable[0].startswith("w.yml:") and action_pins.READ_AS in unreadable[0]


def test_pins_does_not_flag_a_step_name_or_a_comment_that_mentions_uses():
    text = (
        "jobs:\n"
        "  a:\n"
        "    steps:\n"
        "      # uses: evil/action@main\n"
        "      - name: Explain what uses: means\n"
        "        run: echo uses\n"
        '      - run: echo "uses: x"\n'
        f"      - uses: actions/checkout@{SHA_A} # v4.1.1\n"
        "        # uses: evil/action@main\n"
        "        with:\n"
        "          path: uses\n"
    )
    found, unreadable = action_pins.pins(text, "w.yml")
    assert unreadable == []
    assert found == [("w.yml:8", "actions/checkout", SHA_A, "v4.1.1")]


def test_pins_reads_a_job_level_reusable_workflow_pin():
    text = (
        "jobs:\n"
        "  call:\n"
        f"    uses: owner/repo/.github/workflows/build.yml@{SHA_B} # v2.3.4\n"
        "  local:\n"
        "    uses: ./.github/workflows/other.yml\n"
    )
    found, unreadable = action_pins.pins(text, "w.yml")
    assert unreadable == []
    assert found == [("w.yml:3", "owner/repo/.github/workflows/build.yml", SHA_B, "v2.3.4")]
    assert action_pins.repository(found[0][1]) == "owner/repo"
    assert action_pins.repository("actions/checkout") == "actions/checkout"
    assert action_pins.repository("github/codeql-action/init") == "github/codeql-action"


def test_workflow_files_include_both_yaml_extensions(tmp_path):
    folder = tmp_path / ".github/workflows"
    folder.mkdir(parents=True)
    for name in ("b.yaml", "a.yml", "c.txt", "d.yml.bak", "e.YML"):
        (folder / name).write_text("name: x\n")
    (folder / "sub.yml").mkdir()
    names = [path.name for path in action_pins.workflow_files(tmp_path)]
    assert names == sorted(["a.yml", "b.yaml", "sub.yml"]), names


def test_the_command_fails_for_an_unreadable_uses_line_in_any_workflow(
    monkeypatch, tmp_path, capsys
):
    folder = tmp_path / ".github/workflows"
    folder.mkdir(parents=True)
    (folder / "ok.yml").write_text(f"      - uses: actions/checkout@{SHA_A} # v4.1.1\n")
    # workflow_files() binds its default root when the module loads, so point it at tmp_path.
    listing = action_pins.workflow_files
    monkeypatch.setattr(action_pins, "ROOT", tmp_path)
    monkeypatch.setattr(action_pins, "workflow_files", lambda root=tmp_path: listing(root))
    monkeypatch.setattr(action_pins, "github", lambda path, token=None: pytest.fail(path))
    monkeypatch.setattr(action_pins, "problems", lambda pinned, resolve: [])
    assert action_pins.main() == 0
    (folder / "later.yaml").write_text("      - {uses: actions/checkout@main}\n")
    assert action_pins.main() == 1
    assert "later.yaml:1:" in capsys.readouterr().out


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


ELF = b"\x7fELF\x02\x01\x01" + bytes(20)


def make_tree(root):
    """Programs under root/bin and root/sbin: ELF files and others, some installed by a package."""
    bin_dir, sbin_dir = root / "bin", root / "sbin"
    bin_dir.mkdir()
    sbin_dir.mkdir()
    (bin_dir / "copied-in").write_bytes(ELF)
    (bin_dir / "packaged").write_bytes(ELF)
    (bin_dir / "script").write_bytes(b"#!/bin/sh\necho hi\n")
    (bin_dir / "tiny").write_bytes(b"\x7fEL")
    (bin_dir / "empty").write_bytes(b"")
    (bin_dir / "subdirectory").mkdir()
    (bin_dir / "alias-of-copied-in").symlink_to(bin_dir / "copied-in")
    (bin_dir / "alias-of-packaged").symlink_to(bin_dir / "packaged")
    (bin_dir / "dangling").symlink_to(root / "missing")
    (sbin_dir / "admin-tool").write_bytes(ELF)
    return bin_dir, sbin_dir


def test_dpkg_files_lists_the_real_paths_of_the_files_packages_installed(tmp_path):
    root = tmp_path.resolve()
    bin_dir, _ = make_tree(root)
    info = root / "info"
    info.mkdir()
    (info / "tool.list").write_text(
        f"/.\n{bin_dir / 'packaged'}\n{bin_dir / 'alias-of-packaged'}\n{bin_dir / 'gone'}\n\n"
    )
    (info / "other.list").write_text(f"{bin_dir / 'script'}\n")
    (info / "tool.md5sums").write_text(f"x  {bin_dir / 'copied-in'}\n")
    # dpkg's lists also name the root directory ("/."); a directory is never a program.
    assert image_contents.dpkg_files(info) == {
        "/",
        str(bin_dir / "packaged"),
        str(bin_dir / "script"),
    }
    assert image_contents.dpkg_files(root / "no-such-folder") == set()


def test_unpackaged_programs_reports_an_elf_file_no_package_installed(tmp_path):
    root = tmp_path.resolve()
    bin_dir, sbin_dir = make_tree(root)
    owned = {str(bin_dir / "packaged")}
    found = image_contents.unpackaged_programs([str(bin_dir), str(sbin_dir)], owned)
    # A link is reported as the file it points at, once; a package's file (reached through a
    # link too), a script, a short or empty file, a directory and a dangling link are not.
    assert found == sorted([str(bin_dir / "copied-in"), str(sbin_dir / "admin-tool")])


def test_unpackaged_programs_skips_a_listed_file_a_script_and_a_missing_directory(tmp_path):
    root = tmp_path.resolve()
    bin_dir, sbin_dir = make_tree(root)
    owned = {str(bin_dir / "packaged"), str(bin_dir / "copied-in"), str(sbin_dir / "admin-tool")}
    assert image_contents.unpackaged_programs([str(bin_dir), str(root / "none")], owned) == []
    assert image_contents.unpackaged_programs([], set()) == []
    only_scripts = root / "scripts"
    only_scripts.mkdir()
    (only_scripts / "run").write_bytes(b"#!/usr/bin/env python\n")
    assert image_contents.unpackaged_programs([str(only_scripts)], set()) == []


def test_unpackaged_programs_resolves_a_link_into_a_package_directory(tmp_path):
    root = tmp_path.resolve()
    bin_dir, _ = make_tree(root)
    elsewhere = root / "opt"
    elsewhere.mkdir()
    (elsewhere / "vendor-tool").write_bytes(ELF)
    (bin_dir / "vendor-tool").symlink_to(elsewhere / "vendor-tool")
    found = image_contents.unpackaged_programs([str(bin_dir)], {str(bin_dir / "packaged")})
    assert str(elsewhere / "vendor-tool") in found and str(bin_dir / "vendor-tool") not in found
    owned = {str(elsewhere / "vendor-tool"), str(bin_dir / "packaged")}
    assert str(elsewhere / "vendor-tool") not in image_contents.unpackaged_programs(
        [str(bin_dir)], owned
    )


def test_the_probe_prints_json_in_the_shape_the_coverage_check_reads(monkeypatch, capsys, tmp_path):
    (tmp_path / "os-release").write_text(OS_RELEASE)
    (tmp_path / "status").write_text(DPKG_STATUS)
    original = image_contents.os_release, image_contents.deb_packages
    monkeypatch.setattr(image_contents, "os_release", lambda: original[0](tmp_path / "os-release"))
    monkeypatch.setattr(image_contents, "deb_packages", lambda: original[1](tmp_path / "status"))
    monkeypatch.setattr(image_contents, "dpkg_files", lambda: {"/usr/bin/owned"})
    searched = []

    def programs(directories, owned):
        searched.append((list(directories), owned))
        return ["/usr/local/bin/uv"]

    monkeypatch.setattr(image_contents, "unpackaged_programs", programs)
    monkeypatch.setenv("PATH", "/app/.venv/bin::/usr/bin")
    image_contents.main()
    contents = json.loads(capsys.readouterr().out)
    assert set(contents) == {"os", "python", "deb", "pypi", "programs"}
    assert contents["programs"] == ["/usr/local/bin/uv"]
    assert searched == [
        (
            [
                "/app/.venv/bin",
                "/usr/bin",
                "/usr/local/sbin",
                "/usr/local/bin",
                "/usr/sbin",
                "/sbin",
                "/bin",
            ],
            {"/usr/bin/owned"},
        )
    ]
    assert contents["os"]["id"] == "debian" and contents["deb"]["libssl3"] == ["1:3.5.1-1"]
    coverage = load("sbom_coverage")
    problems, _ = coverage.check_image(
        {"components": []}, contents, {"project": {"dependencies": ["pytest"]}}, {"packages": {}}
    )
    assert not any("incomplete" in problem for problem in problems)
