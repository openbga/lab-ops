"""The scripts under tools/security/ that the security workflow runs: the Grype gate (which proves
its settings can fail before trusting a clean scan), the action pin check, the image probe and
the builder of the secret scan's gitleaks configuration. The
workflow's steps are pinned by tests/test_security_checks.py; these hold each script's behaviour."""

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
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
gitleaks_config = load("gitleaks_config")

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
    # The canary also calls a reusable workflow by its path; its advisory names the repository.
    workflow = "kartverket/github-workflows/.github/workflows/run-trivy.yml"
    assert workflow in listed and "kartverket/github-workflows" not in listed
    assert "kartverket/github-workflows" in prepared
    assert "kartverket/github-workflows" in gate.GATES["dependencies"]["canary"]
    assert "kartverket/github-workflows" not in gate.GATES["image"]["canary"]


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


WORKFLOW_KIND = {"name": "syft:package:type", "value": "github-action-workflow"}


def test_the_action_kinds_are_the_two_syft_types_for_github_actions():
    assert gate.ACTION_KINDS == {"github-action", "github-action-workflow"}


def test_with_repository_actions_adds_the_repository_of_a_reusable_workflow():
    name = "kartverket/github-workflows/.github/workflows/run-trivy.yml"
    called = {
        "bom-ref": "wf",
        "type": "library",
        "name": name,
        "version": "v2.7.4",
        "purl": "pkg:github/kartverket/github-workflows@v2.7.4#.github/workflows/run-trivy.yml",
        "properties": [WORKFLOW_KIND],
    }
    original = {"components": [called]}
    before = json.dumps(original)
    prepared = gate.with_repository_actions(original)
    assert json.dumps(original) == before
    assert prepared["components"][0] == called
    (added,) = prepared["components"][1:]
    assert (added["name"], added["version"]) == ("kartverket/github-workflows", "v2.7.4")
    # Advisories for GitHub Actions are matched on the `github-action` type.
    assert added["purl"] == "pkg:github/kartverket/github-workflows@v2.7.4"
    assert added["properties"] == [ACTION] and added["bom-ref"] == "repository-of-wf"
    # Once per version, and not again when the repository itself is listed.
    twice = {"components": [called, {**called, "bom-ref": "wf2", "name": name + "x"}]}
    assert len(gate.with_repository_actions(twice)["components"]) == 3
    listed = {"components": [called, action_component("kartverket/github-workflows", "v2.7.4")]}
    assert gate.with_repository_actions(listed)["components"] == listed["components"]
    # A path-named component of another kind is left alone.
    other = {**called, "properties": [{"name": "syft:package:type", "value": "binary"}]}
    assert gate.with_repository_actions({"components": [other]})["components"] == [other]


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


def test_action_files_hold_the_workflows_and_every_action_definition(tmp_path):
    for relative in (
        ".github/workflows/a.yml",
        ".github/workflows/b.yaml",
        ".github/actions/fetch/action.yml",
        "tools/x/y/action.yaml",
        "action.yml",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("name: x\n")
    skipped = [".git", ".venv", "node_modules", "build", "staticfiles", ".local"]
    for folder in [*skipped, "app/node_modules/dep", ".local/worktrees/w"]:
        for name in ("action.yml", "action.yaml"):
            path = tmp_path / folder / "deep" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("name: x\n")
    (tmp_path / "not-action.yml").write_text("name: x\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/action.yml.bak").write_text("name: x\n")
    found = [path.relative_to(tmp_path).as_posix() for path in action_pins.action_files(tmp_path)]
    assert found[:2] == [".github/workflows/a.yml", ".github/workflows/b.yaml"], found
    assert sorted(found[2:]) == [
        ".github/actions/fetch/action.yml",
        "action.yml",
        "tools/x/y/action.yaml",
    ], found
    assert action_pins.SKIPPED_DIRS == set(skipped)


def test_action_files_of_the_repository_include_the_workflows():
    names = {path.name for path in action_pins.action_files(ROOT)}
    assert {"security.yml", "secrets.yml", "codeql.yml"} <= names
    assert action_pins.action_files(ROOT) == action_pins.action_files()


def test_a_skipped_directory_above_the_root_does_not_hide_its_actions(tmp_path):
    # A checkout under a `.local` or `build` directory (a worktree, a CI workspace) is still read.
    root = tmp_path / ".local" / "build" / "repo"
    (root / ".github/actions/x").mkdir(parents=True)
    (root / ".github/actions/x/action.yml").write_text("name: x\n")
    assert [p.name for p in action_pins.action_files(root)] == ["action.yml"]


def test_pins_reads_the_actions_a_composite_action_uses(tmp_path):
    text = (
        "name: fetch\ndescription: d\nruns:\n  using: composite\n  steps:\n"
        f"    - uses: actions/download-artifact@{SHA_A} # v4.1.1\n"
        "    - uses: evil/action@main\n"
    )
    found, unreadable = action_pins.pins(text, "fetch/action.yml")
    assert [(repo, tag) for _, repo, _, tag in found] == [("actions/download-artifact", "v4.1.1")]
    assert len(unreadable) == 1 and unreadable[0].startswith("fetch/action.yml:7:")


def test_the_command_reads_and_fails_for_a_composite_action(monkeypatch, tmp_path, capsys):
    folder = tmp_path / ".github/workflows"
    folder.mkdir(parents=True)
    (folder / "ok.yml").write_text(f"      - uses: actions/checkout@{SHA_A} # v4.1.1\n")
    action = tmp_path / ".github/actions/fetch/action.yml"
    action.parent.mkdir(parents=True)
    action.write_text(f"runs:\n  steps:\n    - uses: actions/download-artifact@{SHA_B} # v4.1.8\n")
    seen = []
    monkeypatch.setattr(action_pins, "ROOT", tmp_path)
    monkeypatch.setattr(action_pins, "github", lambda path, token=None: pytest.fail(path))
    monkeypatch.setattr(action_pins, "problems", lambda pinned, resolve: seen.append(pinned) or [])
    assert action_pins.main() == 0
    assert "All 2 action pins" in capsys.readouterr().out
    assert {repo for _, repo, _, _ in seen[0]} == {"actions/checkout", "actions/download-artifact"}
    action.write_text("runs:\n  steps:\n    - uses: evil/action@main\n")
    assert action_pins.main() == 1
    assert ".github/actions/fetch/action.yml:3:" in capsys.readouterr().out


def test_the_command_reports_a_misnamed_pin_in_a_composite_action(monkeypatch, tmp_path, capsys):
    action = tmp_path / ".github/actions/fetch/action.yml"
    action.parent.mkdir(parents=True)
    (tmp_path / ".github/workflows").mkdir()
    action.write_text(f"runs:\n  steps:\n    - uses: actions/download-artifact@{SHA_A} # v4.1.8\n")
    monkeypatch.setattr(action_pins, "ROOT", tmp_path)
    monkeypatch.setattr(action_pins, "tag_commit", lambda repo, tag, fetch=None: "f" * 40)
    assert action_pins.main() == 1
    output = capsys.readouterr().out
    assert "action.yml:3: actions/download-artifact@6b208ae046db is commented v4.1.8" in output


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


# Adapted: dpkg_files() now returns {real path: recorded MD5 or None}; the *.md5sums files give
# the digests.
def test_dpkg_files_maps_the_real_paths_of_the_files_packages_installed_to_their_md5(tmp_path):
    root = tmp_path.resolve()
    bin_dir, _ = make_tree(root)
    info = root / "info"
    info.mkdir()
    (info / "tool.list").write_text(
        f"/.\n{bin_dir / 'packaged'}\n{bin_dir / 'alias-of-packaged'}\n{bin_dir / 'gone'}\n\n"
    )
    (info / "other.list").write_text(f"{bin_dir / 'script'}\n")
    relative = str(bin_dir / "packaged").lstrip("/")
    gone = str(bin_dir / "gone").lstrip("/")
    (info / "tool.md5sums").write_text(f"{'a' * 32}  {relative}\n{'b' * 32}  {gone}\nnoise\n")
    # dpkg's lists also name the root directory ("/."); a directory is never a program.
    assert image_contents.dpkg_files(info) == {
        "/": None,
        str(bin_dir / "packaged"): "a" * 32,
        str(bin_dir / "script"): None,
    }
    assert image_contents.dpkg_files(root / "no-such-folder") == {}


def test_dpkg_files_takes_a_file_only_named_in_md5sums_from_that_file(tmp_path):
    root = tmp_path.resolve()
    only = program(root / "usr/bin/only-in-md5sums")
    info = root / "info"
    info.mkdir()
    (info / "tool.md5sums").write_text(f"{'c' * 32}  {only.lstrip('/')}\n")
    assert image_contents.dpkg_files(info) == {only: "c" * 32}


# Adapted: unpackaged_programs() now walks a whole tree (`root`, with `skip` for the pseudo
# filesystems) instead of a list of directories, and looks at execute bits and names.
def program(path, magic=ELF, mode=0o755):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(magic)
    path.chmod(mode)
    return str(path)


def test_unpackaged_programs_finds_an_executable_elf_file_anywhere_under_the_root(tmp_path):
    root = tmp_path.resolve()
    expected = sorted(
        [
            program(root / "opt/report-tool/bin/report-tool"),
            program(root / "usr/local/bin/uv"),
            program(root / "srv/deep/er/tool.sound"),  # `.so` followed by other letters
            program(root / "home/user/.cache/tool", mode=0o700),
            program(root / "tool-at-the-top"),
        ]
    )
    # Found at their own paths (a directory's files before its subdirectories).
    assert sorted(image_contents.unpackaged_programs(str(root), {})) == expected


def test_unpackaged_programs_ignores_what_is_not_an_executable_program(tmp_path):
    root = tmp_path.resolve()
    program(root / "opt/data/no-execute-bit", mode=0o644)
    program(root / "opt/lib/libx.so.1")
    program(root / "opt/lib/libx.so")
    program(root / "opt/lib/libx.so.1.2.3", mode=0o644)
    program(root / "opt/bin/script", b"#!/bin/sh\necho hi\n")
    program(root / "opt/bin/tiny", b"\x7fEL")
    program(root / "opt/bin/empty", b"")
    (root / "opt/bin/subdirectory.d").mkdir()
    (root / "opt/bin/link").symlink_to(root / "opt/bin/script")
    (root / "opt/bin/dangling").symlink_to(root / "missing")
    found = program(root / "opt/bin/real-tool")
    (root / "opt/bin/alias-of-real-tool").symlink_to(found)
    assert image_contents.unpackaged_programs(str(root), {}) == [found]


def test_unpackaged_programs_ignores_the_files_a_package_installed(tmp_path):
    root = tmp_path.resolve()
    packaged = program(root / "usr/bin/packaged")
    copied = program(root / "usr/local/bin/copied-in")
    (root / "usr/bin/alias-of-packaged").symlink_to(packaged)
    (root / "usr/local/bin/link-to-packaged").symlink_to(packaged)
    # Adapted: `owned` is {real path: recorded MD5 or None}; None means no digest to compare.
    assert image_contents.unpackaged_programs(str(root), {packaged: None}) == [copied]
    assert image_contents.unpackaged_programs(str(root), {packaged: None, copied: None}) == []


def test_a_link_into_a_package_directory_is_judged_by_the_file_it_points_at(tmp_path):
    root = tmp_path.resolve()
    target = program(root / "opt/vendor/vendor-tool")
    (root / "usr/local/bin").mkdir(parents=True)
    (root / "usr/local/bin/vendor-tool").symlink_to(target)
    assert image_contents.unpackaged_programs(str(root), {}) == [target]
    # The link is skipped as a link; ownership is judged on the real path of the file.
    assert image_contents.unpackaged_programs(str(root), {target: None}) == []


def md5_of(data):
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def test_a_packaged_program_whose_content_matches_its_recorded_md5_is_not_listed(tmp_path):
    root = tmp_path.resolve()
    packaged = program(root / "usr/bin/packaged")
    assert image_contents.unpackaged_programs(str(root), {packaged: md5_of(ELF)}) == []


def test_a_packaged_program_whose_content_differs_from_its_recorded_md5_is_listed(tmp_path):
    root = tmp_path.resolve()
    # A binary copied over a packaged file: the path is owned but the content is not.
    changed = ELF + b"copied over by the Dockerfile"
    replaced = program(root / "usr/bin/packaged", changed)
    untouched = program(root / "usr/bin/untouched")
    owned = {replaced: md5_of(ELF), untouched: md5_of(ELF)}
    assert image_contents.unpackaged_programs(str(root), owned) == [replaced]
    # Control: the same file is not listed once its recorded MD5 is that of its content.
    owned[replaced] = md5_of(changed)
    assert image_contents.unpackaged_programs(str(root), owned) == []


def test_an_owned_program_without_an_md5_entry_is_treated_as_packaged(tmp_path):
    root = tmp_path.resolve()
    packaged = program(root / "usr/bin/packaged", ELF + b"any content at all")
    assert image_contents.unpackaged_programs(str(root), {packaged: None}) == []


def test_a_link_to_a_packaged_program_is_judged_by_the_md5_of_the_file_it_points_at(tmp_path):
    root = tmp_path.resolve()
    packaged = program(root / "usr/bin/packaged")
    (root / "usr/bin/alias").symlink_to(packaged)
    assert image_contents.unpackaged_programs(str(root), {packaged: md5_of(ELF)}) == []
    assert image_contents.unpackaged_programs(str(root), {packaged: md5_of(b"other")}) == [packaged]


def test_unpackaged_programs_does_not_enter_the_skipped_directories(tmp_path):
    root = tmp_path.resolve()
    for folder in ("proc", "sys", "dev"):
        program(root / folder / "hidden")
    program(root / "usr/proc/visible")  # only the exact paths are skipped
    kept = program(root / "opt/tool")
    assert image_contents.PSEUDO_FILESYSTEMS == ("/proc", "/sys", "/dev")
    skip = tuple(str(root / folder) for folder in ("proc", "sys", "dev"))
    assert sorted(image_contents.unpackaged_programs(str(root), {}, skip=skip)) == sorted(
        [kept, str(root / "usr/proc/visible")]
    )
    assert len(image_contents.unpackaged_programs(str(root), {}, skip=())) == 5


def test_unpackaged_programs_skips_a_directory_it_cannot_read_and_a_missing_root(tmp_path):
    root = tmp_path.resolve()
    assert image_contents.unpackaged_programs(str(root / "none"), set()) == []
    assert image_contents.unpackaged_programs(str(root), {}) == []
    unreadable = root / "private"
    kept = program(root / "opt/tool")
    program(unreadable / "tool")
    unreadable.chmod(0)
    try:
        found = image_contents.unpackaged_programs(str(root), {})
    finally:
        unreadable.chmod(0o755)
    # As root (the probe runs with --user 0) nothing is unreadable, so the tool is found too.
    assert sorted(found) == sorted(
        [kept, str(unreadable / "tool")] if os.geteuid() == 0 else [kept]
    )


def test_the_probe_prints_json_in_the_shape_the_coverage_check_reads(monkeypatch, capsys, tmp_path):
    (tmp_path / "os-release").write_text(OS_RELEASE)
    (tmp_path / "status").write_text(DPKG_STATUS)
    original = image_contents.os_release, image_contents.deb_packages
    monkeypatch.setattr(image_contents, "os_release", lambda: original[0](tmp_path / "os-release"))
    monkeypatch.setattr(image_contents, "deb_packages", lambda: original[1](tmp_path / "status"))
    monkeypatch.setattr(image_contents, "dpkg_files", lambda: {"/usr/bin/owned": None})
    searched = []

    def programs(root, owned):
        searched.append((root, owned))
        return ["/usr/local/bin/uv"]

    monkeypatch.setattr(image_contents, "unpackaged_programs", programs)
    image_contents.main()
    contents = json.loads(capsys.readouterr().out)
    assert set(contents) == {"os", "python", "deb", "pypi", "programs"}
    assert contents["programs"] == ["/usr/local/bin/uv"]
    # Adapted: the whole tree is searched from "/", not PATH and fixed directories.
    assert searched == [("/", {"/usr/bin/owned": None})]
    assert contents["os"]["id"] == "debian" and contents["deb"]["libssl3"] == ["1:3.5.1-1"]
    coverage = load("sbom_coverage")
    problems, _ = coverage.check_image(
        {"components": []}, contents, {"project": {"dependencies": ["pytest"]}}, {"packages": {}}
    )
    assert not any("incomplete" in problem for problem in problems)


# -------------------------------------------------------------------------- gitleaks_config

DEFAULT_TOML = """\
# This is a synthetic stand-in for gitleaks' default configuration, with the same layout.
# Rules and allowlists are defined within this file.

title = "gitleaks config"

# minVersion indicates the minimum Gitleaks version required to use this config.
minVersion = "v8.25.0"

# TODO: change to [[allowlists]]
[allowlist]
description = "global allow lists"
paths = [
    '''gitleaks\\.toml''',
    '''(?i)\\.(?:bmp|gif|jpe?g|png|svg|tiff?)$''',
    '''(?:^|/)vendor/modules\\.txt$''',
]
regexes = [
    '''(?i)^true|false|null$''',
    '''^(?i:true|false|null)$''',
]
stopwords = [
    "000000",
    "abcdef",
    "example",
]

[[rules]]
id = "first-rule"
description = "A rule without exceptions"
regex = '''\\b(first_[a-z0-9]{8})\\b'''
keywords = ["first_"]

[[rules]]
id = "second-rule"
description = "A rule with its own exceptions, one of them for named files"
regex = '''\\b(second_[a-z0-9]{8})\\b'''
keywords = ["second_"]
[[rules.allowlists]]
paths = [
    '''(?:^|/)docs/.*\\.md$''',
]
regexes = [
    '''.+EXAMPLE$''',
]

[[rules]]
id = "third-rule"
description = "A rule with stopwords"
regex = '''\\b(third_[a-z0-9]{8})\\b'''
keywords = ["third_"]
[[rules.allowlists]]
stopwords = ["third_dummy00"]
"""

PROJECT_TOML = """\
# The project's own exceptions.

[extend]
useDefault = true

[[allowlists]]
description = "Synthetic values used by the tests"
regexTarget = "secret"
regexes = [
  # A made-up password.
  '''^a-made-up-value-1$''',
  '''^SIM-KEY-1122$''',
]
"""

DEFAULT = tomllib.loads(DEFAULT_TOML)
PROJECT = tomllib.loads(PROJECT_TOML)


def built(default_text=DEFAULT_TOML, project_text=PROJECT_TOML):
    return tomllib.loads(gitleaks_config.build(default_text, project_text))


def test_the_synthetic_default_has_the_layout_the_builder_reads():
    assert DEFAULT["allowlist"]["paths"] and len(DEFAULT["allowlist"]["paths"]) == 3
    assert len(DEFAULT["rules"]) == 3 and "paths" in DEFAULT["rules"][1]["allowlists"][0]


def test_the_built_configuration_parses_with_exactly_the_expected_keys():
    result = built()
    assert set(result) == {"title", "minVersion", "allowlists", "rules"}
    assert result["title"] == "gitleaks config" and result["minVersion"] == "v8.25.0"


def test_no_global_allowlist_lists_paths_and_the_default_one_keeps_the_rest():
    result = built()
    assert not any("paths" in allowlist for allowlist in result["allowlists"])
    first = result["allowlists"][0]
    assert first == {
        "description": "global allow lists",
        "regexes": DEFAULT["allowlist"]["regexes"],
        "stopwords": DEFAULT["allowlist"]["stopwords"],
    }


def test_the_rules_are_unchanged_including_a_rule_level_allowlist_with_paths():
    result = built()
    assert result["rules"] == DEFAULT["rules"]
    assert result["rules"][1]["allowlists"][0]["paths"] == [r"(?:^|/)docs/.*\.md$"]


def test_the_projects_exact_value_allowlist_follows_the_default_one():
    result = built()
    assert result["allowlists"][1:] == PROJECT["allowlists"]
    assert result["allowlists"][1]["regexes"] == ["^a-made-up-value-1$", "^SIM-KEY-1122$"]
    assert len(result["allowlists"]) == 2


def test_the_extend_table_is_dropped():
    text = gitleaks_config.build(DEFAULT_TOML, PROJECT_TOML)
    assert "extend" not in tomllib.loads(text) and "useDefault" not in text
    assert "[allowlist]" not in text.replace("[[allowlists]]", "")


def test_a_default_without_an_allowlist_table_fails():
    text = DEFAULT_TOML.replace("[allowlist]\n", "")
    with pytest.raises(ValueError, match="exactly one"):
        gitleaks_config.build(text, PROJECT_TOML)


def test_a_default_with_two_allowlist_tables_fails():
    text = DEFAULT_TOML.replace("[[rules]]", '[allowlist]\ndescription = "again"\n\n[[rules]]', 1)
    assert text != DEFAULT_TOML
    with pytest.raises(ValueError):
        gitleaks_config.build(text, PROJECT_TOML)


def test_a_project_without_allowlists_fails():
    with pytest.raises(ValueError, match="has no"):
        gitleaks_config.build(DEFAULT_TOML, "[extend]\nuseDefault = true\n")


def test_a_default_that_is_not_toml_fails():
    with pytest.raises(tomllib.TOMLDecodeError):
        gitleaks_config.build(DEFAULT_TOML + "\n[[rules]\n", PROJECT_TOML)


def good_text():
    return gitleaks_config.build(DEFAULT_TOML, PROJECT_TOML)


def test_check_accepts_the_built_text():
    gitleaks_config.check(good_text(), DEFAULT, PROJECT)


@pytest.mark.parametrize(
    ("label", "tamper"),
    [
        (
            "a global allowlist with paths",
            lambda t: t.replace(
                'description = "global allow lists"',
                'description = "global allow lists"\npaths = ["x"]',
                1,
            ),
        ),
        ("a rule changed", lambda t: t.replace('id = "first-rule"', 'id = "first-ruleX"', 1)),
        (
            "a rule dropped",
            lambda t: t.replace('id = "third-rule"', 'id = "third-rule"\nenabled = 1', 1),
        ),
        ("an unexpected top-level key", lambda t: "extra = 1\n" + t),
        ("the extend table kept", lambda t: "[extend]\nuseDefault = true\n" + t),
        ("a stopword dropped", lambda t: t.replace('    "abcdef",\n', "", 1)),
        ("the project's value changed", lambda t: t.replace("^SIM-KEY-1122$", "^.*$")),
        ("the project's allowlist missing", lambda t: t.split("\n[[allowlists]]")[0] + "\n"),
        ("a project allowlist doubled", lambda t: t + t[t.rindex("[[allowlists]]") :]),
        ("a rule-level allowlist path dropped", lambda t: t.replace("docs/", "doc/", 1)),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_check_rejects_a_tampered_result(label, tamper):
    tampered = tamper(good_text())
    assert tampered != good_text(), label
    with pytest.raises(ValueError):
        gitleaks_config.check(tampered, DEFAULT, PROJECT)


def test_check_rejects_a_result_without_any_allowlist():
    with pytest.raises(ValueError, match="first allowlist"):
        gitleaks_config.check(
            'title = "gitleaks config"\nminVersion = "v8.25.0"\nallowlists = []\n'
            + "".join(f"[[rules]]\nid = {rule['id']!r}\n" for rule in DEFAULT["rules"]),
            DEFAULT,
            PROJECT,
        )


def test_main_prints_the_built_configuration(tmp_path, capsys):
    default, project = tmp_path / "default.toml", tmp_path / "project.toml"
    default.write_text(DEFAULT_TOML)
    project.write_text(PROJECT_TOML)
    assert gitleaks_config.main([str(default), str(project)]) == 0
    captured = capsys.readouterr()
    assert captured.out == good_text() and not captured.err


@pytest.mark.parametrize("arguments", [[], ["one"], ["one", "two", "three"]])
def test_main_exits_2_on_the_wrong_number_of_arguments(arguments, capsys):
    assert gitleaks_config.main(arguments) == 2
    captured = capsys.readouterr()
    assert not captured.out and "gitleaks_config.py" in captured.err


def test_main_raises_for_an_unusable_default_and_writes_nothing(tmp_path, capsys):
    default, project = tmp_path / "default.toml", tmp_path / "project.toml"
    default.write_text(DEFAULT_TOML.replace("[allowlist]\n", ""))
    project.write_text(PROJECT_TOML)
    with pytest.raises(ValueError):
        gitleaks_config.main([str(default), str(project)])
    assert capsys.readouterr().out == ""


def test_the_command_line_prints_to_standard_output_and_exits_2_on_misuse(tmp_path):
    script = str(ROOT / "tools/security/gitleaks_config.py")
    default, project = tmp_path / "default.toml", tmp_path / "project.toml"
    default.write_text(DEFAULT_TOML)
    project.write_text(PROJECT_TOML)
    done = subprocess.run(
        [sys.executable, "-I", script, str(default), str(project)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout == good_text() and not done.stderr
    misuse = subprocess.run([sys.executable, "-I", script], capture_output=True, text=True)
    assert misuse.returncode == 2 and not misuse.stdout


# The real default configuration for the pinned release, found next to the scanners the review
# tools directory holds; these tests never download it.
TOOLS = Path(os.environ.get("LABOPS_REVIEW_TOOLS", "/tmp/labops-rsc-tools"))
GITLEAKS_VERSION_PINNED = "8.30.1"


def real_default():
    candidates = [os.environ.get("LABOPS_GITLEAKS_DEFAULT_CONFIG"), TOOLS / "gitleaks-default.toml"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    pytest.skip("the default gitleaks configuration is not next to the scanners")


def real_gitleaks():
    bundled = TOOLS / "bin/gitleaks"
    path = str(bundled) if bundled.is_file() else shutil.which("gitleaks")
    if not path:
        pytest.skip("gitleaks is not available")
    return path


def test_the_real_default_configuration_is_the_pinned_one_and_builds():
    default = real_default().read_text()
    workflow = (ROOT / ".github/workflows/secrets.yml").read_text()
    pinned = re.search(r"GITLEAKS_CONFIG_SHA256: ([0-9a-f]{64})", workflow).group(1)
    if hashlib.sha256(default.encode()).hexdigest() != pinned:
        pytest.skip("the default configuration found is for another gitleaks release")
    text = gitleaks_config.build(default, (ROOT / ".gitleaks.toml").read_text())
    result = tomllib.loads(text)
    original = tomllib.loads(default)
    assert result["rules"] == original["rules"] and len(result["rules"]) > 100
    assert not any("paths" in allowlist for allowlist in result["allowlists"])
    assert {key: value for key, value in original["allowlist"].items() if key != "paths"} == (
        result["allowlists"][0]
    )
    assert original["allowlist"]["paths"], "control: the default does list paths"
    assert (
        result["allowlists"][1:]
        == tomllib.loads((ROOT / ".gitleaks.toml").read_text())["allowlists"]
    )
    assert any(
        "paths" in allowlist for rule in result["rules"] for allowlist in rule.get("allowlists", [])
    ), "rule-level exceptions for named files are kept"


def canary_files(directory):
    """Three files gitleaks' default configuration partly skips by name, each holding a token
    assembled at run time (no letter l: the default passes over values containing 'false')."""
    import secrets as secret_source

    alphabet = "ABCDEFGHIJKMNOPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz0123456789"

    def token():
        return "ghp_" + "".join(secret_source.choice(alphabet) for _ in range(36))

    (directory / "static/vendor").mkdir(parents=True)
    (directory / "settings_local.py").write_text(f'token = "{token()}"\n')
    (directory / "static/vendor/package-lock.json").write_text(f'{{"note": "{token()}"}}\n')
    (directory / "badge.svg").write_text(f"<svg><!-- {token()} --></svg>\n")


def reported_files(binary, config, directory, report):
    done = subprocess.run(
        [
            binary,
            "dir",
            "--ignore-gitleaks-allow",
            "--redact",
            "--no-banner",
            "--config",
            str(config),
            "--report-format",
            "json",
            "--report-path",
            str(report),
            str(directory),
        ],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 1, done.stderr
    return {Path(finding["File"]).name for finding in json.loads(report.read_text())}


def test_the_built_configuration_reports_all_three_canary_files_and_the_default_only_one(tmp_path):
    default = real_default()
    binary = real_gitleaks()
    pinned = re.search(
        r"GITLEAKS_CONFIG_SHA256: ([0-9a-f]{64})",
        (ROOT / ".github/workflows/secrets.yml").read_text(),
    ).group(1)
    if hashlib.sha256(default.read_bytes()).hexdigest() != pinned:
        pytest.skip("the default configuration found is for another gitleaks release")
    config = tmp_path / "gitleaks.toml"
    config.write_text(
        gitleaks_config.build(default.read_text(), (ROOT / ".gitleaks.toml").read_text())
    )
    canary = tmp_path / "canary"
    canary_files(canary)
    expected = {"settings_local.py", "package-lock.json", "badge.svg"}
    assert reported_files(binary, config, canary, tmp_path / "built.json") == expected
    assert reported_files(binary, default, canary, tmp_path / "default.json") == {
        "settings_local.py"
    }
