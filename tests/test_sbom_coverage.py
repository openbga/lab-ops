"""The security workflow trusts a clean scan only after tools/security/sbom_coverage.py has shown
that the SBOM lists what the scan must cover: a scanner that cannot read a lockfile or a package
database reports no vulnerabilities rather than an error. These tests hold the script to that."""

import importlib.util
import json
import shutil
import tomllib
from pathlib import Path

import pytest
from django.conf import settings

ROOT = Path(settings.BASE_DIR)
SPEC = importlib.util.spec_from_file_location(
    "sbom_coverage", ROOT / "tools/security/sbom_coverage.py"
)
coverage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coverage)
PINS = importlib.util.spec_from_file_location(
    "check_action_pins", ROOT / "tools/security/check_action_pins.py"
)
action_pins = importlib.util.module_from_spec(PINS)
PINS.loader.exec_module(action_pins)

UV_LOCK = {
    "package": [
        {"name": "lab-ops", "version": "0.1.0", "source": {"virtual": "."}},
        {"name": "Django", "version": "5.2.19", "source": {"registry": "https://pypi.org/simple"}},
        {"name": "psycopg_binary", "version": "3.3.8", "source": {"registry": "x"}},
        {"name": "pytest", "version": "9.0.0", "source": {"registry": "x"}},
    ]
}
PYPROJECT = {"project": {"dependencies": ["Django>=5.2.19,<5.3", "psycopg-binary[pool]>=3"]}}
VENDOR = {
    "lockfileVersion": 3,
    "packages": {
        "": {"dependencies": {"htmx.org": "2.0.11"}},
        "node_modules/htmx.org": {"version": "2.0.11"},
        "node_modules/@fontsource-variable/overpass": {"version": "5.3.0"},
    },
}
# What image_contents.py reports for a small image: Debian 13, Python 3.14.10, two Debian
# packages (one with an epoch) and the Python packages the image holds, which include a
# transitive dependency (asgiref) and tools the project does not name.
CONTENTS = {
    "os": {"id": "debian", "version_id": "13"},
    "python": "3.14.10",
    "deb": {"libc6": ["2.41-12"], "libssl3": ["1:3.5.1-1"]},
    "pypi": {
        "Django": ["5.2.19"],
        "psycopg_binary": ["3.3.8"],
        "asgiref": ["3.8.1"],
        "pip": ["25.1"],
    },
    # A program no Debian package installed: the Dockerfile copies uv into /bin.
    "programs": ["/usr/local/bin/uv"],
}
PROGRAMS = ["/usr/local/bin/uv"]
SHA_A = "6b208ae046db98c579e8a3aa621ab581ff575935"
SHA_B = "fa0a91b85d4f404e444e00e005971372dc801d16"
# The workflows whose pinned actions a dependency listing must hold: one action, one action named
# by a path inside its repository, and a local action, which is not listed anywhere.
WORKFLOWS = {
    "w.yml": (
        "jobs:\n"
        "  a:\n"
        "    steps:\n"
        f"      - uses: actions/checkout@{SHA_A} # v7.0.1\n"
        f"      - uses: github/codeql-action/init@{SHA_B} # v4.39.3\n"
        f"      - uses: github/codeql-action/analyze@{SHA_B} # v4.39.3\n"
        "      - uses: ./.github/actions/local\n"
    )
}


def sbom(*purls, operating_system=("debian", "13"), paths=()):
    """A CycloneDX document: a component for each package URL, one found at each path (the
    properties Syft writes), and the operating system."""
    components = [{"purl": purl} for purl in purls]
    components += [
        {
            "name": path.rsplit("/", 1)[-1],
            "properties": [
                {"name": "syft:package:type", "value": "binary"},
                {"name": "syft:location:0:path", "value": path},
            ],
        }
        for path in paths
    ]
    if operating_system:
        name, version = operating_system
        components.append({"type": "operating-system", "name": name, "version": version})
    return {"components": components}


LOCKED_PURLS = [
    "pkg:pypi/django@5.2.19",
    "pkg:pypi/psycopg-binary@3.3.8",
    "pkg:pypi/pytest@9.0.0",
    "pkg:npm/htmx.org@2.0.11",
    "pkg:npm/%40fontsource-variable/overpass@5.3.0",
    "pkg:github/actions/checkout@v7.0.1",
    "pkg:github/github/codeql-action@v4.39.3#init",
]
LOCKED = sbom(*LOCKED_PURLS)
IMAGE_PURLS = [
    "pkg:pypi/django@5.2.19",
    "pkg:pypi/psycopg-binary@3.3.8",
    "pkg:pypi/asgiref@3.8.1",
    "pkg:pypi/pip@25.1",
    "pkg:npm/htmx.org@2.0.11",
    "pkg:npm/%40fontsource-variable/overpass@5.3.0",
    "pkg:deb/debian/libc6@2.41-12?arch=amd64&distro=debian-13",
    "pkg:deb/debian/libssl3@1%3A3.5.1-1?arch=amd64&distro=debian-13",
    "pkg:generic/python@3.14.10",
]
IMAGE = sbom(*IMAGE_PURLS, paths=PROGRAMS)


def image_sbom(*purls, **changes):
    """A listing of the image that holds the program paths unless `paths` says otherwise."""
    return sbom(*purls, **{"paths": PROGRAMS, **changes})


def without(purls, *fragments):
    """The package URLs that hold none of the fragments."""
    return [p for p in purls if not any(f in p for f in fragments)]


def check_locked(document, uv_lock=UV_LOCK, workflows=WORKFLOWS):
    return coverage.check_locked(document, uv_lock, VENDOR, workflows)


def check_image(document, contents=CONTENTS, pyproject=PYPROJECT):
    return coverage.check_image(document, contents, pyproject, VENDOR)


# ------------------------------------------------------------------------- locked mode


def test_a_complete_locked_listing_passes():
    problems, expected = check_locked(LOCKED)
    assert problems == [] and expected == 7  # three Python, two npm and two actions


def test_the_project_itself_is_not_expected():
    assert "lab-ops" not in coverage.locked_python(UV_LOCK)


def test_a_missing_locked_package_is_reported_by_name_and_version():
    problems, _ = check_locked(sbom(*without(LOCKED_PURLS, "pytest")))
    assert problems == ["pypi pytest 9.0.0: not in the SBOM"]


def test_a_different_version_is_reported():
    document = sbom("pkg:pypi/django@5.2.0", *without(LOCKED_PURLS, "django"))
    problems, _ = check_locked(document)
    assert problems == ["pypi django 5.2.19: the SBOM lists 5.2.0"]


def test_vendored_browser_packages_are_required():
    document = sbom(
        "pkg:pypi/django@5.2.19",
        "pkg:pypi/psycopg-binary@3.3.8",
        "pkg:pypi/pytest@9.0.0",
        "pkg:github/actions/checkout@v7.0.1",
        "pkg:github/github/codeql-action@v4.39.3",
    )
    problems, _ = check_locked(document)
    assert problems == [
        "npm @fontsource-variable/overpass 5.3.0: not in the SBOM",
        "npm htmx.org 2.0.11: not in the SBOM",
    ]


def test_an_empty_sbom_fails():
    problems, expected = check_locked({"components": []})
    assert len(problems) == expected == 7


def test_a_package_locked_at_two_versions_needs_both_listed():
    forked = {
        "package": [
            *UV_LOCK["package"],
            {"name": "Django", "version": "4.2.24", "source": {"registry": "x"}},
        ]
    }
    assert coverage.locked_python(forked)["django"] == {"5.2.19", "4.2.24"}
    problems, expected = check_locked(LOCKED, forked)
    assert problems == ["pypi django 4.2.24: the SBOM lists 5.2.19"] and expected == 8
    both = sbom("pkg:pypi/django@4.2.24", *LOCKED_PURLS)
    assert check_locked(both, forked)[0] == []
    only_old = sbom("pkg:pypi/django@4.2.24", *without(LOCKED_PURLS, "django"))
    assert check_locked(only_old, forked)[0] == ["pypi django 5.2.19: the SBOM lists 4.2.24"]


def test_names_compare_as_python_normalizes_them():
    document = sbom("pkg:pypi/Django@5.2.19", "pkg:pypi/psycopg_binary@3.3.8")
    found = coverage.sbom_packages(document)
    assert ("pypi", "django") in found and ("pypi", "psycopg-binary") in found
    assert not coverage.missing(found, "pypi", {"django": {"5.2.19"}, "psycopg-binary": {"3.3.8"}})


def test_package_urls_decode_by_ecosystem():
    found = coverage.sbom_packages(
        sbom(
            "pkg:deb/debian/libssl3@1%3A2.38-1?arch=amd64",
            "pkg:npm/%40scope/name@1.0.0",
            "pkg:generic/python@3.14.10",
            "pkg:golang/example.com/x@v1.0.0",
            "pkg:github/actions/checkout@v7",
            "pkg:github/github/codeql-action@v4.39.3#init",
        )
    )
    assert found == {
        ("deb", "libssl3"): {"1:2.38-1"},
        ("npm", "@scope/name"): {"1.0.0"},
        ("generic", "python"): {"3.14.10"},
        ("github", "actions/checkout"): {"v7"},
        ("github", "github/codeql-action"): {"v4.39.3"},
    }


# ----------------------------------------------------------- pinned actions, locked mode


def test_the_pinned_actions_are_read_from_the_workflows_by_repository():
    actions, problems = coverage.pinned_actions(WORKFLOWS)
    assert problems == []
    assert actions == {"actions/checkout": {"v7.0.1"}, "github/codeql-action": {"v4.39.3"}}
    more = {
        **WORKFLOWS,
        "x.yaml": f"      - uses: actions/checkout@{SHA_B} # v7.1.0\n",
    }
    assert coverage.pinned_actions(more)[0]["actions/checkout"] == {"v7.0.1", "v7.1.0"}


def test_an_action_missing_from_the_sbom_is_reported_by_name_and_release():
    problems, _ = check_locked(sbom(*without(LOCKED_PURLS, "actions/checkout")))
    assert problems == ["github actions/checkout v7.0.1: not in the SBOM"]


def test_a_sub_path_action_counts_for_its_repository():
    # Syft lists github/codeql-action/init as pkg:github/github/codeql-action@v4.39.3#init, and
    # the workflow also uses .../analyze at the same release: one entry covers both.
    assert check_locked(LOCKED)[0] == []
    problems, _ = check_locked(sbom(*without(LOCKED_PURLS, "codeql-action")))
    assert problems == ["github github/codeql-action v4.39.3: not in the SBOM"]
    stale = sbom("pkg:github/github/codeql-action@v4.39.0#init", *without(LOCKED_PURLS, "codeql"))
    assert check_locked(stale)[0] == ["github github/codeql-action v4.39.3: the SBOM lists v4.39.0"]


def test_an_action_pinned_at_two_releases_needs_both_listed():
    workflows = {
        **WORKFLOWS,
        "other.yml": f"      - uses: actions/checkout@{SHA_B} # v7.1.0\n",
    }
    problems, expected = check_locked(LOCKED, workflows=workflows)
    assert problems == ["github actions/checkout v7.1.0: the SBOM lists v7.0.1"]
    assert expected == 8


def test_a_uses_line_the_pin_reader_cannot_read_fails_the_listing_check():
    workflows = {"bad.yml": "      - uses:\n          actions/checkout@v7\n"}
    problems, _ = check_locked(LOCKED, workflows=workflows)
    assert len(problems) == 1 and problems[0].startswith("bad.yml:1:")
    assert "one line" in problems[0]
    flow = {"flow.yaml": "      - {uses: actions/checkout@v7}\n"}
    assert check_locked(LOCKED, workflows=flow)[0][0].startswith("flow.yaml:1:")


def test_no_workflows_means_no_actions_are_expected():
    problems, expected = check_locked(sbom(*without(LOCKED_PURLS, "pkg:github")), workflows={})
    assert problems == [] and expected == 5


# -------------------------------------------------------------------------- image mode


def test_a_complete_image_listing_passes():
    problems, expected = check_image(IMAGE)
    assert problems == []
    # deb, pypi, Python, operating system, vendored and programs
    assert expected == 2 + 4 + 1 + 1 + 2 + 1


def test_a_missing_debian_package_is_reported():
    problems, _ = check_image(image_sbom(*without(IMAGE_PURLS, "libc6")))
    assert problems == ["deb libc6 2.41-12: not in the SBOM"]


def test_a_missing_transitive_python_package_is_reported():
    problems, _ = check_image(image_sbom(*without(IMAGE_PURLS, "asgiref")))
    assert problems == ["pypi asgiref 3.8.1: not in the SBOM"]


def test_a_missing_python_interpreter_is_reported():
    problems, _ = check_image(image_sbom(*without(IMAGE_PURLS, "pkg:generic/python")))
    assert problems == ["generic python 3.14.10: not in the SBOM"]


def test_a_missing_operating_system_component_is_reported():
    problems, _ = check_image(image_sbom(*IMAGE_PURLS, operating_system=None))
    assert problems == ["operating system debian 13: not in the SBOM"]
    problems, _ = check_image(image_sbom(*IMAGE_PURLS, operating_system=("alpine", "3.22")))
    assert problems == ["operating system debian 13: not in the SBOM"]


def test_an_operating_system_listed_with_a_point_release_matches():
    assert check_image(image_sbom(*IMAGE_PURLS, operating_system=("debian", "13.1")))[0] == []
    problems, _ = check_image(image_sbom(*IMAGE_PURLS, operating_system=("debian", "130")))
    assert problems


def test_a_missing_vendored_package_is_reported():
    problems, _ = check_image(image_sbom(*without(IMAGE_PURLS, "pkg:npm/htmx.org")))
    assert problems == ["npm htmx.org 2.0.11: not in the SBOM"]


def test_a_deb_version_with_an_epoch_decodes():
    found = coverage.sbom_packages(sbom("pkg:deb/debian/libssl3@1%3A2.38?arch=amd64"))
    assert found[("deb", "libssl3")] == {"1:2.38"}
    contents = {**CONTENTS, "deb": {"libc6": ["2.41-12"], "libssl3": ["1:2.38"]}}
    document = image_sbom(*without(IMAGE_PURLS, "libssl3"), "pkg:deb/debian/libssl3@1%3A2.38")
    assert check_image(document, contents)[0] == []
    stale = image_sbom(*without(IMAGE_PURLS, "libssl3"), "pkg:deb/debian/libssl3@2.38")
    assert check_image(stale, contents)[0] == ["deb libssl3 1:2.38: the SBOM lists 2.38"]


def test_python_names_in_the_listing_are_normalised():
    document = image_sbom(*without(IMAGE_PURLS, "psycopg"), "pkg:pypi/psycopg_binary@3.3.8")
    assert check_image(document)[0] == []


def test_a_package_the_image_holds_at_two_versions_needs_both():
    contents = {**CONTENTS, "pypi": {**CONTENTS["pypi"], "Django": ["5.2.19", "4.2.24"]}}
    problems, _ = check_image(IMAGE, contents)
    assert problems == ["pypi django 4.2.24: the SBOM lists 5.2.19"]


def test_a_runtime_dependency_not_installed_in_the_image_fails():
    pyproject = {"project": {"dependencies": ["Django>=5", "gunicorn>=26"]}}
    problems, _ = check_image(IMAGE, pyproject=pyproject)
    assert problems == ["runtime dependencies not installed in the image: ['gunicorn']"]


@pytest.mark.parametrize(
    "contents",
    [
        {},
        {**CONTENTS, "deb": {}},
        {**CONTENTS, "pypi": {}},
        {**CONTENTS, "python": ""},
        {**CONTENTS, "os": {}},
        {**CONTENTS, "os": {"id": "debian"}},
        {key: value for key, value in CONTENTS.items() if key != "os"},
        {key: value for key, value in CONTENTS.items() if key != "programs"},
    ],
    ids=[
        "empty",
        "no deb",
        "no pypi",
        "no python",
        "no os",
        "no os version",
        "os key missing",
        "programs key missing",
    ],
)
def test_an_incomplete_image_listing_fails(contents):
    problems, expected = check_image(IMAGE, contents)
    assert expected == 0 and len(problems) == 1 and "image listing is incomplete" in problems[0]


def test_a_program_no_package_installed_needs_an_sbom_entry_found_in_it():
    problems, _ = check_image(sbom(*IMAGE_PURLS))
    assert problems == ["program /usr/local/bin/uv: no SBOM entry was found in it"]
    elsewhere = image_sbom(*IMAGE_PURLS, paths=["/usr/bin/other"])
    assert check_image(elsewhere)[0] == problems


def test_the_locations_of_an_sbom_are_every_syft_location_property():
    document = {
        "components": [
            {"properties": [{"name": "syft:location:0:path", "value": "/a"}]},
            {
                "properties": [
                    {"name": "syft:location:1:path", "value": "/b"},
                    {"name": "syft:location:1:layerID", "value": "sha256:x"},
                    {"name": "syft:package:type", "value": "/not-a-location"},
                ]
            },
            {"purl": "pkg:pypi/x@1"},
        ]
    }
    assert coverage.locations(document) == {"/a", "/b"}


def test_an_image_without_programs_outside_the_packages_still_needs_the_key():
    contents = {**CONTENTS, "programs": []}
    assert check_image(sbom(*IMAGE_PURLS), contents)[0] == []
    assert check_image(sbom(*IMAGE_PURLS), {**CONTENTS, "programs": None})[0] != []


def test_every_missing_program_is_reported():
    contents = {**CONTENTS, "programs": ["/usr/local/bin/uv", "/usr/local/bin/uvx"]}
    problems, expected = check_image(image_sbom(*IMAGE_PURLS), contents)
    assert problems == ["program /usr/local/bin/uvx: no SBOM entry was found in it"]
    assert expected == 2 + 4 + 1 + 1 + 2 + 2


# ------------------------------------------------------------------- command and files


def write(tmp_path, name, value):
    path = tmp_path / name
    path.write_text(json.dumps(value))
    return str(path)


def test_the_command_exits_1_and_names_what_is_missing_in_locked_mode(tmp_path, capsys):
    path = write(tmp_path, "empty.cdx.json", {"components": []})
    assert coverage.main([path, "--locked"]) == 1
    output = capsys.readouterr().out
    assert "does not list" in output and "pypi django" in output


def test_the_command_exits_1_in_image_mode_for_an_incomplete_listing(tmp_path, capsys):
    sbom_path = write(tmp_path, "image.cdx.json", {"components": []})
    contents = write(tmp_path, "contents.json", {})
    assert coverage.main([sbom_path, "--image", contents]) == 1
    assert "image listing is incomplete" in capsys.readouterr().out


def test_the_command_requires_exactly_one_mode(tmp_path):
    path = write(tmp_path, "x.cdx.json", {"components": []})
    for argv in ([path], [path, "--locked", "--image", path], [path, "--runtime"]):
        with pytest.raises(SystemExit) as stopped:
            coverage.main(argv)
        assert stopped.value.code == 2


def repository_lists():
    uv_lock = tomllib.loads((ROOT / "uv.lock").read_text())
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    vendor = json.loads((ROOT / "static/vendor/package-lock.json").read_text())
    return uv_lock, pyproject, vendor


def test_the_command_exits_0_for_a_listing_of_the_repository_locks(tmp_path, capsys):
    uv_lock, _, vendor = repository_lists()
    purls = [
        f"pkg:pypi/{name}@{version}"
        for name, versions in coverage.locked_python(uv_lock).items()
        for version in versions
    ] + [
        f"pkg:npm/{name.replace('@', '%40')}@{next(iter(versions))}"
        for name, versions in coverage.vendored(vendor).items()
    ]
    # Every action the repository's workflows pin, as Syft lists them (a path in the repository
    # becomes a purl fragment).
    workflows = {path.name: path.read_text() for path in action_pins.workflow_files(ROOT)}
    purls += [
        f"pkg:github/{repo}@{tag}#init"
        for repo, tags in coverage.pinned_actions(workflows)[0].items()
        for tag in tags
    ]
    path = write(tmp_path, "deps.cdx.json", sbom(*purls))
    assert coverage.main([path, "--locked"]) == 0
    assert "lists all" in capsys.readouterr().out
    without_actions = write(tmp_path, "no-actions.cdx.json", sbom(*without(purls, "pkg:github")))
    assert coverage.main([without_actions, "--locked"]) == 1
    assert "github actions/checkout" in capsys.readouterr().out


def composite_repository(tmp_path):
    """A repository root with the real lockfiles, a workflow pinning actions/checkout, and a local
    composite action pinning actions/download-artifact; its listing without the two actions."""
    root = tmp_path / "repo"
    (root / "static/vendor").mkdir(parents=True)
    shutil.copy(ROOT / "uv.lock", root / "uv.lock")
    shutil.copy(ROOT / "static/vendor/package-lock.json", root / "static/vendor/package-lock.json")
    (root / ".github/workflows").mkdir(parents=True)
    (root / ".github/workflows/w.yml").write_text(
        f"jobs:\n  a:\n    steps:\n      - uses: actions/checkout@{SHA_A} # v7.0.1\n"
        "      - uses: ./.github/actions/fetch\n"
    )
    action = root / ".github/actions/fetch/action.yml"
    action.parent.mkdir(parents=True)
    action.write_text(
        "name: fetch\ndescription: d\nruns:\n  using: composite\n  steps:\n"
        f"    - uses: actions/download-artifact@{SHA_B} # v4.1.8\n"
    )
    uv_lock, _, vendor = repository_lists()
    purls = [
        f"pkg:pypi/{name}@{version}"
        for name, versions in coverage.locked_python(uv_lock).items()
        for version in versions
    ] + [
        f"pkg:npm/{name.replace('@', '%40')}@{next(iter(versions))}"
        for name, versions in coverage.vendored(vendor).items()
    ]
    return root, action, purls


def test_the_command_requires_the_actions_a_composite_action_uses(tmp_path, monkeypatch, capsys):
    root, _, purls = composite_repository(tmp_path)
    monkeypatch.setattr(coverage, "ROOT", root)
    checkout = "pkg:github/actions/checkout@v7.0.1"
    download = "pkg:github/actions/download-artifact@v4.1.8"
    # Without the composite action's pin read, the first listing would pass.
    workflows_only = {p.name: p.read_text() for p in action_pins.workflow_files(root)}
    assert coverage.pinned_actions(workflows_only)[0] == {"actions/checkout": {"v7.0.1"}}
    only_workflow = write(tmp_path, "a.cdx.json", sbom(*purls, checkout))
    assert coverage.main([only_workflow, "--locked"]) == 1
    assert "github actions/download-artifact v4.1.8: not in the SBOM" in capsys.readouterr().out
    both = write(tmp_path, "b.cdx.json", sbom(*purls, checkout, download))
    assert coverage.main([both, "--locked"]) == 0
    assert "lists all" in capsys.readouterr().out
    # The release in the composite action's comment is the one the listing must hold.
    stale = write(
        tmp_path,
        "c.cdx.json",
        sbom(*purls, checkout, "pkg:github/actions/download-artifact@v4.1.1"),
    )
    assert coverage.main([stale, "--locked"]) == 1
    assert "actions/download-artifact v4.1.8: the SBOM lists v4.1.1" in capsys.readouterr().out


def test_the_command_fails_for_a_line_it_cannot_read_in_a_composite_action(
    tmp_path, monkeypatch, capsys
):
    root, action, purls = composite_repository(tmp_path)
    monkeypatch.setattr(coverage, "ROOT", root)
    action.write_text(action.read_text() + "    - uses: evil/action@main\n")
    listing = write(
        tmp_path,
        "d.cdx.json",
        sbom(
            *purls,
            "pkg:github/actions/checkout@v7.0.1",
            "pkg:github/actions/download-artifact@v4.1.8",
        ),
    )
    assert coverage.main([listing, "--locked"]) == 1
    assert ".github/actions/fetch/action.yml:7:" in capsys.readouterr().out
    # An action file in a directory the pin script skips is not read.
    skipped = root / "node_modules/dep/action.yml"
    skipped.parent.mkdir(parents=True)
    skipped.write_text("runs:\n  steps:\n    - uses: evil/action@main\n")
    action.write_text(action.read_text().replace("    - uses: evil/action@main\n", ""))
    assert coverage.main([listing, "--locked"]) == 0


def test_the_repository_lockfiles_parse_and_name_the_runtime_dependencies():
    uv_lock, pyproject, vendor = repository_lists()
    locked = coverage.locked_python(uv_lock)
    runtime = coverage.runtime_names(pyproject)
    assert {"django", "django-allauth", "psycopg", "gunicorn", "whitenoise"} <= runtime
    assert runtime <= set(locked)
    vendored = coverage.vendored(vendor)
    assert vendored["htmx.org"] and vendored["bootstrap"]
    assert "lab-ops" not in locked
