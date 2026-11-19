"""Run one of the security workflow's Grype gates, after proving that its settings still fail.

    python3 tools/security/grype_gate.py dependencies --grype PATH SBOM
    python3 tools/security/grype_gate.py image --grype PATH SBOM

Each gate first scans canary.cdx.json, which lists package versions with published
vulnerabilities, using exactly the arguments and the .grype.yaml it then uses for SBOM. The canary
must fail and report each package the gate is meant to catch; otherwise the settings, the
configuration or the vulnerability database would let a vulnerable package pass, and the gate
stops before trusting a clean result (SECURITY.md, "What is checked"). The exit status is the
real scan's: 0 clean, 2 a vulnerability at the gate's level, anything else an error.
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".grype.yaml"
CANARY = Path(__file__).resolve().with_name("canary.cdx.json")

# What fails each gate, and the canary packages each must report: django 5.2.0 has fixed High
# and Critical advisories, ecdsa 0.19.2 a High advisory with no fixed release, bootstrap 4.0.0
# fixed Medium advisories, actions/download-artifact v4.1.1 a fixed High advisory, and
# github/codeql-action v3.28.0, listed as its init action, a fixed High advisory.
GATES = {
    "dependencies": {
        "args": ["--fail-on", "low"],
        "canary": {
            "django",
            "ecdsa",
            "bootstrap",
            "actions/download-artifact",
            "github/codeql-action",
        },
    },
    "image": {
        "args": ["--only-fixed", "--fail-on", "high"],
        "canary": {"django"},
    },
}


def with_repository_actions(sbom):
    """A copy of a CycloneDX SBOM in which each action named by a path inside its repository
    (github/codeql-action/init) is also listed under the repository's name, the name advisories
    for GitHub Actions use. Without it, no advisory would match such an action."""
    components = list(sbom.get("components", []))
    listed = {(component.get("name"), component.get("version")) for component in components}
    for component in sbom.get("components", []):
        kinds = {
            prop.get("value")
            for prop in component.get("properties", [])
            if prop.get("name") == "syft:package:type"
        }
        name, version = component.get("name", ""), component.get("version", "")
        if "github-action" not in kinds or name.count("/") < 2:
            continue
        repository = "/".join(name.split("/")[:2])
        if (repository, version) in listed:
            continue
        listed.add((repository, version))
        components.append(
            {
                "bom-ref": f"repository-of-{component.get('bom-ref', name)}",
                "type": "library",
                "name": repository,
                "version": version,
                "purl": f"pkg:github/{repository}@{version}",
                "properties": [{"name": "syft:package:type", "value": "github-action"}],
            }
        )
    return {**sbom, "components": components}


def prepare(sbom, folder, name):
    """Write the SBOM to scan, with the repository names of its actions, and return its path."""
    path = Path(folder) / name
    path.write_text(json.dumps(with_repository_actions(json.loads(Path(sbom).read_text()))))
    return path


def grype_command(grype, gate, sbom, output):
    """The Grype command line of a gate."""
    return [
        grype,
        f"sbom:{sbom}",
        "--config",
        str(CONFIG),
        *GATES[gate]["args"],
        *output,
    ]


def canary_problems(gate, returncode, report):
    """Why the canary scan shows the gate could pass a vulnerable package (empty when sound)."""
    reported = {match["artifact"]["name"] for match in report.get("matches", [])}
    problems = []
    if returncode != 2:
        problems.append(f"Grype exited {returncode} on the canary, not 2 (vulnerability found)")
    unreported = sorted(GATES[gate]["canary"] - reported)
    if unreported:
        problems.append(f"the canary's vulnerable packages were not reported: {unreported}")
    return problems


def run(gate, grype, sbom, runner=subprocess.run):
    with tempfile.TemporaryDirectory() as scratch:
        report_file = Path(scratch) / "canary.json"
        canary = runner(
            grype_command(
                grype,
                gate,
                prepare(CANARY, scratch, "canary.cdx.json"),
                ["--output", f"json={report_file}"],
            ),
            cwd=scratch,
            capture_output=True,
            text=True,
        )
        text = report_file.read_text() if report_file.exists() else ""
        problems = canary_problems(gate, canary.returncode, json.loads(text) if text else {})
        if problems:
            print(f"The {gate} gate cannot be trusted:", *problems, sep="\n  ")
            print(canary.stdout, canary.stderr, sep="\n")
            return 1
        print(f"Canary: the {gate} gate reports {sorted(GATES[gate]['canary'])}.", flush=True)
        target = prepare(sbom, scratch, "scan.cdx.json")
        return runner(
            grype_command(grype, gate, target, ["--output", "table"]), cwd=scratch
        ).returncode


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("gate", choices=sorted(GATES))
    parser.add_argument("--grype", required=True, help="path to the grype executable")
    parser.add_argument("sbom", type=Path, help="CycloneDX JSON file to scan")
    args = parser.parse_args(argv)
    return run(args.gate, args.grype, args.sbom.resolve())


if __name__ == "__main__":
    sys.exit(main())
