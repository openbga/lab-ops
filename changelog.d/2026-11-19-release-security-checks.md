---
date: 2026-11-19
type: added
impact: low
title: Security checks on every change
---
Each change is checked for vulnerable components, committed secrets and unsafe code, and each release lists its components.

## Technical notes

Pull requests and a weekly run on `main` go through CodeQL static analysis, a Grype audit of
`uv.lock`, the vendored browser packages and the workflows' actions, a gitleaks scan of the
history, and a Grype scan of the container image. Each run keeps a software bill of materials of
the image (SPDX 2.3 and CycloneDX 1.6) for 90 days, and each release published on GitHub carries
it as release assets. The security policy (`SECURITY.md`) lists what fails each check and what
is not checked. The vendored browser packages are now also recorded in
`static/vendor/package-lock.json`, which is served with the other static files. Nothing is
installed from it, and deployments need no change.
