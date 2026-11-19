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
vulnerability published later for something already in use is reported. The workflows are
[`security.yml`](.github/workflows/security.yml) and [`codeql.yml`](.github/workflows/codeql.yml).

| Check | What it covers | What fails it |
| --- | --- | --- |
| Static analysis (CodeQL, security-extended queries) | The application's Python code and the GitHub Actions workflows. Tests and browser journeys are left out | A pull request that adds an alert at the repository's failure threshold. Alerts are listed in the repository's Security tab |
| Dependency audit (Grype) | Every package in `uv.lock`, in all dependency groups; the browser packages under `static/vendor/`, recorded in its `package-lock.json`; the actions the workflows use | Any published vulnerability, fixed or not |
| Secret scan (gitleaks) | Every commit in the history of the change | A match for gitleaks' default rules that is not a listed synthetic test value |
| Container image (Grype) | The image the Dockerfile builds: operating-system packages, Python and the installed Python packages | A vulnerability rated High or Critical by its advisory for which a fixed version is published |

Before scanning, each scan checks that it lists every package it must cover (every locked and
vendored package, or the image's runtime and vendored packages) and fails if one is missing, so a
lockfile the scanner cannot read does not pass as a clean result.

Each run also builds a software bill of materials (SBOM) of the container image, in SPDX 2.3 and
CycloneDX 1.6 JSON, and keeps it with the run for 90 days as the `sbom` artifact. When a release
is published on GitHub, its SBOM is attached to the release as `lab-ops-<tag>.spdx.json` and
`lab-ops-<tag>.cdx.json`.

A finding that does not apply, or that has no fix to apply yet, is recorded with the reason and
reviewed like code: a vulnerability in [`.grype.yaml`](.grype.yaml), tied to one package version so
that it lapses when the package changes, and a synthetic test value in
[`.gitleaks.toml`](.gitleaks.toml). The actions the workflows use are pinned to commits and kept
current by Dependabot. gitleaks is pinned to a version and its checksum.

## What is not checked

- There is no penetration test and no bug bounty.
- No deployment is scanned, including the public demonstration. Its host, configuration, secrets,
  network and database belong to its operator.
- The running application is tested by its test suites only, not by a dynamic scanner.
- Advisories are matched by package name and version. A vulnerability with no published advisory
  is not found, and a match does not prove the vulnerable code is reachable.
- In the container image, a vulnerability rated below High, or with no published fix, does not
  fail the check. To list every finding, run Grype on the SBOM.
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
