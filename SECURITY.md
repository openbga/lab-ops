# Security policy

## Reporting a vulnerability

Please report security problems privately. Do not open a public issue, pull request or
discussion for them.

Submit a [private vulnerability report](https://github.com/openbga/lab-ops/security/advisories/new).
Include the affected component and commit, the role you used, what you observed and how a
maintainer can verify it. Include a suggested fix if you have one.

Use synthetic data only. Never include real laboratory, client, patient or personal data in a report,
and do not test against a deployment you do not operate.

Reports are handled on a best-effort basis by the maintainers. We will confirm receipt, keep
you informed while we investigate, and credit you in the advisory unless you prefer otherwise.
There is no bug bounty.

## Supported versions

Maintainers publish security fixes on `main`. Deployments should track that branch and apply
updates promptly.

## What is checked

Every pull request goes through the checks below, and they run again each week on `main`, so a
vulnerability published later for something already in use is reported. The secret scan also runs
on every push of a branch or a tag. The workflows are [`security.yml`](.github/workflows/security.yml),
[`secrets.yml`](.github/workflows/secrets.yml) and [`codeql.yml`](.github/workflows/codeql.yml).

| Check | What it covers | What fails it |
| --- | --- | --- |
| Static analysis (CodeQL, security-extended queries) | The application's Python code, its own JavaScript under `static/js/`, and the GitHub Actions workflows. Tests, browser journeys and the vendored third-party files are left out | A pull request that adds an alert at the repository's failure threshold. Alerts are listed in the repository's Security tab |
| Dependency audit (Grype) | Every package in `uv.lock`, in all dependency groups; the browser packages under `static/vendor/`, recorded in its `package-lock.json`; the actions the workflows use, directly or through a local composite action, an action or reusable workflow inside a repository (such as `github/codeql-action/init`) under its repository's name | Any published vulnerability, fixed or not, and an action whose pinned commit is not the release its version comment names |
| Secret scan (gitleaks) | Every commit in the history of the change, merge commits included, read as text: file by file, and again as one stream so that no file is skipped by its name | A match for gitleaks' default rules that is not a listed synthetic test value, and any `.gitleaksignore` file |
| Container image (Grype) | The image the Dockerfile builds: the operating system and its packages, Python, the installed Python packages, every other program anywhere in the image that no Debian package installed (such as uv), and the vendored browser packages | A vulnerability rated High or Critical by its advisory for which a fixed version is published |

Before trusting a clean result, each check shows that it could have failed. The listing a scan
reads is compared with what it must contain: every locked and vendored package and every pinned
action, or, for the image, everything the image itself reports (its operating system, Debian
packages, Python, installed Python packages and other programs). Then the scanner's settings are
run against a known finding: package versions with published vulnerabilities, or a generated
token. If the listing is incomplete, or the known finding is not reported, the check fails instead
of passing.

Each run also builds a software bill of materials (SBOM) of the container image, in SPDX 2.3 and
CycloneDX 1.6 JSON, and keeps it with the run for 90 days as the `sbom` artifact once it has passed
the comparison above. When a release is published on GitHub, that SBOM is attached to the release
as `lab-ops-<tag>.spdx.json` and `lab-ops-<tag>.cdx.json`.

A finding that does not apply, or that has no fix to apply yet, is recorded with the reason and
reviewed like code: a vulnerability in [`.grype.yaml`](.grype.yaml), tied to one package version so
that it lapses when the package changes, and a synthetic test value, as one exact value, in
[`.gitleaks.toml`](.gitleaks.toml). Tests reject any other setting in either file. The actions the
workflows use are pinned to commits and kept current by Dependabot. gitleaks is pinned to a
version and its checksum.

## What is not checked

- There is no penetration test and no bug bounty.
- No deployment is scanned, including the public demonstration. Its host, configuration, secrets,
  network and database belong to its operator.
- The running application is tested by its test suites only, not by a dynamic scanner.
- Advisories are matched by package name and version. A vulnerability with no published advisory
  is not found, and a match does not prove the vulnerable code is reachable. The vendored
  third-party files are matched this way only; static analysis does not read them.
- In the container image, a vulnerability rated below High, or with no published fix, does not
  fail the check. To list every finding, run Grype on the SBOM.
- Native libraries bundled inside Python wheels (for example in Pillow or psycopg) are not listed
  on their own. They are covered only through the advisories published for the wheel's package.
- GitHub starts no workflow for a push whose commit message asks it to skip them (`[skip ci]` and
  its variants). Such commits are scanned for secrets when a pull request, a later push or the
  weekly run reaches them; a secret pushed that way and removed before then is not reported.
- The SBOM describes the image built by the check. An image built elsewhere or later can contain
  newer operating-system packages, because the base image tag is updated for security fixes.
- Licences of dependencies are not checked. The SBOM lists those its tool can identify.
- Code scanning needs the repository to be public. A private copy needs GitHub Code Security for
  CodeQL; the other checks need nothing extra.

These checks cover this project's source and image. They are not a vulnerability scan or a
penetration test of a laboratory's systems, and passing them does not show that software is free
of vulnerabilities.

## Scope

In scope: this repository's application code, configuration, container image definition and
deployment Blueprint.

Out of scope: vulnerabilities in a third-party dependency with no impact specific to this
project (report those upstream), findings that require an administrator or database owner to
attack their own deployment, and the configuration of deployments operated by others.

## Notes for operators

Each deployment is operated by the laboratory that runs it. The operator is responsible for
its secrets, HTTPS configuration, user accounts, multi-factor authentication policy, backups and
updates. Two documented design limits matter when assessing risk:

- Authenticator-app secrets and recovery codes are stored unencrypted in the database, so
  database dumps must be encrypted and access to them restricted.
- History tables reject updates and deletes through database triggers, but a database owner or
  superuser can disable them. In the packaged deployment the application's own database role
  owns its tables, so this includes anyone who holds the application's database credentials.
  This is not tamper evidence against database administrators.

See [Operations](docs/operations.md) for both.
