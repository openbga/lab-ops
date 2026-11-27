---
date: 2026-11-27
type: security
impact: high
title: The container image installs Debian's published security updates when it is built
---
The application image now takes Debian's published package updates when it is built, so a security fix is included before the Python base image is rebuilt with it.

## Technical notes

The Dockerfile runs `apt-get update` and `apt-get upgrade` on the `python:3.14.10-slim-bookworm` base before installing the application. This brings in the `libpam` packages `1.5.2-6+deb12u3` from Debian's security archive (CVE-2025-6020, rated High), which the base image did not carry yet. An image built later picks up whatever updates Debian has published by then. Rebuild your image to include them. No new setting or environment variable.
