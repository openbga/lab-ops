"""The scripts under tools/security/ that the security workflow runs: the Grype gate (which proves
its settings can fail before trusting a clean scan), the action pin check, the image probe and
the text window rewriter the secret scan reads its stream through. The
workflow's steps are pinned by tests/test_security_checks.py; these hold each script's behaviour."""

import importlib.util
import io
import json
import os
import random
import subprocess
import sys
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
text_windows = load("text_windows")

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
    assert sorted(image_contents.unpackaged_programs(str(root), set())) == expected


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
    assert image_contents.unpackaged_programs(str(root), set()) == [found]


def test_unpackaged_programs_ignores_the_files_a_package_installed(tmp_path):
    root = tmp_path.resolve()
    packaged = program(root / "usr/bin/packaged")
    copied = program(root / "usr/local/bin/copied-in")
    (root / "usr/bin/alias-of-packaged").symlink_to(packaged)
    (root / "usr/local/bin/link-to-packaged").symlink_to(packaged)
    assert image_contents.unpackaged_programs(str(root), {packaged}) == [copied]
    assert image_contents.unpackaged_programs(str(root), {packaged, copied}) == []


def test_a_link_into_a_package_directory_is_judged_by_the_file_it_points_at(tmp_path):
    root = tmp_path.resolve()
    target = program(root / "opt/vendor/vendor-tool")
    (root / "usr/local/bin").mkdir(parents=True)
    (root / "usr/local/bin/vendor-tool").symlink_to(target)
    assert image_contents.unpackaged_programs(str(root), set()) == [target]
    # The link is skipped as a link; ownership is judged on the real path of the file.
    assert image_contents.unpackaged_programs(str(root), {target}) == []


def test_unpackaged_programs_does_not_enter_the_skipped_directories(tmp_path):
    root = tmp_path.resolve()
    for folder in ("proc", "sys", "dev"):
        program(root / folder / "hidden")
    program(root / "usr/proc/visible")  # only the exact paths are skipped
    kept = program(root / "opt/tool")
    assert image_contents.PSEUDO_FILESYSTEMS == ("/proc", "/sys", "/dev")
    skip = tuple(str(root / folder) for folder in ("proc", "sys", "dev"))
    assert sorted(image_contents.unpackaged_programs(str(root), set(), skip=skip)) == sorted(
        [kept, str(root / "usr/proc/visible")]
    )
    assert len(image_contents.unpackaged_programs(str(root), set(), skip=())) == 5


def test_unpackaged_programs_skips_a_directory_it_cannot_read_and_a_missing_root(tmp_path):
    root = tmp_path.resolve()
    assert image_contents.unpackaged_programs(str(root / "none"), set()) == []
    assert image_contents.unpackaged_programs(str(root), set()) == []
    unreadable = root / "private"
    kept = program(root / "opt/tool")
    program(unreadable / "tool")
    unreadable.chmod(0)
    try:
        found = image_contents.unpackaged_programs(str(root), set())
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
    monkeypatch.setattr(image_contents, "dpkg_files", lambda: {"/usr/bin/owned"})
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
    assert searched == [("/", {"/usr/bin/owned"})]
    assert contents["os"]["id"] == "debian" and contents["deb"]["libssl3"] == ["1:3.5.1-1"]
    coverage = load("sbom_coverage")
    problems, _ = coverage.check_image(
        {"components": []}, contents, {"project": {"dependencies": ["pytest"]}}, {"packages": {}}
    )
    assert not any("incomplete" in problem for problem in problems)


# ------------------------------------------------------------------------ text_windows

WIDTH, OVERLAP = text_windows.WIDTH, text_windows.OVERLAP
STEP = WIDTH - OVERLAP


def rewritten(data):
    out = io.BytesIO()
    text_windows.rewrite(io.BytesIO(data), out)
    return out.getvalue()


def long_line(length, seed=1):
    return random.Random(seed).randbytes(length).replace(b"\n", b"x")


def test_the_window_sizes():
    assert (text_windows.WIDTH, text_windows.OVERLAP) == (20_000, 2_000)


def test_a_short_line_is_unchanged_and_followed_by_a_blank_line():
    assert text_windows.windows(b"+token = 1") == [b"+token = 1"]
    assert text_windows.windows(b"") == [b""]
    exact = b"x" * WIDTH
    assert text_windows.windows(exact) == [exact]
    assert rewritten(b"+a\n+b\n") == b"+a\n\n+b\n\n"
    assert rewritten(b"+a\n+b") == b"+a\n\n+b\n\n", "a last line without a newline"
    assert rewritten(b"") == b""
    assert rewritten(b"\n") == b"\n\n"


def test_every_line_gets_a_blank_line_after_it():
    lines = [b"diff --git a/x b/x", b"+" + b"y" * 50_000, b"+short", b" context"]
    out = rewritten(b"\n".join(lines) + b"\n")
    pieces = out.split(b"\n")
    assert pieces[-2:] == [b"", b""], "ends with a blank line"
    body = pieces[:-1]  # every window is followed by one empty piece
    assert body[1::2] == [b""] * (len(body) // 2) and all(body[0::2])
    assert max(map(len, body)) <= WIDTH


@pytest.mark.parametrize(
    "length",
    [WIDTH + 1, WIDTH + OVERLAP, 2 * WIDTH, STEP * 2 + OVERLAP, STEP * 2 + OVERLAP + 1, 300_000],
)
def test_a_long_line_is_split_into_overlapping_windows_that_cover_it(length):
    line = long_line(length)
    pieces = text_windows.windows(line)
    assert len(pieces) > 1 and all(0 < len(piece) <= WIDTH for piece in pieces)
    starts = [index * STEP for index in range(len(pieces))]
    for start, piece in zip(starts, pieces, strict=True):
        assert piece == line[start : start + WIDTH]
    for before, after in zip(pieces, pieces[1:], strict=False):
        assert before[-OVERLAP:] == after[:OVERLAP], "consecutive windows overlap"
        assert len(before) == WIDTH
    assert starts[0] == 0 and starts[-1] + len(pieces[-1]) == length, "the last reaches the end"
    assert pieces[-1] and line.endswith(pieces[-1])
    # Every slice of at most OVERLAP bytes lies whole in some window (a token cut by one window
    # is whole in the next).
    for first in range(0, length - OVERLAP + 1):
        assert any(
            start <= first and first + OVERLAP <= start + len(piece)
            for start, piece in zip(starts, pieces, strict=True)
        ), first
    for first in list(range(0, length - OVERLAP + 1, 997)) + [length - OVERLAP]:
        for size in (1, 100, OVERLAP):
            wanted = line[first : first + size]
            assert any(wanted in piece for piece in pieces), (first, size)


def test_a_token_across_every_window_boundary_appears_whole():
    line = b"a" * 70_000
    pieces = text_windows.windows(line)
    token = b"ghp_" + b"T" * (OVERLAP - 4)
    for index in range(1, len(pieces)):
        boundary = index * STEP + (WIDTH - STEP)  # where window `index - 1` ends
        planted = line[: boundary - 700] + token + line[boundary - 700 + len(token) :]
        assert any(token in piece for piece in text_windows.windows(planted)), boundary


def test_the_rewritten_stream_holds_each_window_and_a_blank_line():
    line = long_line(300_000)
    pieces = text_windows.windows(line)
    out = rewritten(b"before\n" + line + b"\nafter\n")
    assert out == b"before\n\n" + b"".join(piece + b"\n\n" for piece in pieces) + b"after\n\n"
    assert b"\n\n".join(out.split(b"\n\n")) == out
    assert all(len(chunk) <= WIDTH for chunk in out.split(b"\n\n"))


def test_bytes_that_are_not_utf_8_pass_through_unchanged():
    raw = b"+caf\xe9 \xff\xfe\x80\x00 binary\r"
    assert rewritten(raw + b"\n") == raw + b"\n\n"
    huge = b"\xff\xfe" * 60_000
    out = rewritten(huge + b"\n")
    assert out == b"".join(piece + b"\n\n" for piece in text_windows.windows(huge))
    assert out.replace(b"\n\n", b"")  # a window is cut by bytes, never decoded
    with pytest.raises(UnicodeDecodeError):
        raw.decode()


def test_the_command_rewrites_standard_input_to_standard_output():
    script = text_windows.__file__
    data = b"+one\n+two \xff\n" + b"+" + b"z" * 25_000 + b"\n"
    done = subprocess.run(
        [sys.executable, "-I", script], input=data, capture_output=True, check=True
    )
    assert done.stdout == rewritten(data) and not done.stderr
