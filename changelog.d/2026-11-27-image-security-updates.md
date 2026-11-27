---
date: 2026-11-27
type: security
impact: high
title: The container image installs Debian's published security updates when it is built
---
The application image now takes Debian's published package updates when it is built, so a security fix is included before the Python base image is rebuilt with it.

## Technical notes

The Dockerfile runs `apt-get update` and `apt-get upgrade` on the `python:3.14.10-slim-bookworm` base before installing the application. This brings in the `libpam` packages `1.5.2-6+deb12u3` from Debian's security archive (CVE-2025-6020, rated High), which the base image did not carry yet. Docker keeps this step in its build cache: an image built again on the same machine reuses the packages of the earlier build. To bring in updates Debian has published since, build without the cache and with a fresh base image (`docker build --pull --no-cache`). A host that builds from a clean cache, as CI does, gets them anyway. No new setting or environment variable.
