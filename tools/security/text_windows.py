"""Rewrite a text stream so that a scanner reading it in chunks never splits a match.

`gitleaks stdin` reads 100,000 bytes, then up to 25,000 more until it meets a blank line
(maxPeekSize in gitleaks 8.30.1), and scans each chunk on its own. A git diff has almost no blank
lines, so a long stream would be cut at an arbitrary place, possibly inside a secret. This cuts the
stream into segments of WIDTH bytes that overlap the next by OVERLAP bytes, each followed by a blank
line. WIDTH stays under that look-ahead, so a chunk ends only at a segment's end or at a blank line
already in the text, which no secret contains, and any match up to OVERLAP bytes long (a
multi-line private key or a PGP key with subkeys included) lies whole inside some segment. A
longer secret may be missed. Standard library only.

    git log -p --text HEAD | python3 tools/security/text_windows.py | gitleaks stdin ...
"""

import sys

WIDTH = 24_000
OVERLAP = 20_000
STEP = WIDTH - OVERLAP


def segments(stream, width=WIDTH, overlap=OVERLAP):
    """Yield overlapping segments of the byte stream `stream` (a binary file object)."""
    step = width - overlap
    buffer = stream.read(width)
    if not buffer:
        return
    while True:
        yield buffer[:width]
        more = stream.read(step)
        if not more:
            return
        buffer = buffer[step:] + more


def rewrite(source, target):
    for segment in segments(source):
        target.write(segment + b"\n\n")


if __name__ == "__main__":
    rewrite(sys.stdin.buffer, sys.stdout.buffer)
