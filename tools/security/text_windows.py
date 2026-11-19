"""Rewrite a text stream so that a scanner reading it in chunks never splits a match.

`gitleaks stdin` reads about 100 kB, then up to 25 kB more until it meets a blank line, and scans
each chunk on its own; a git diff line is never blank, so a long stream would be cut inside lines.
This writes every line followed by a blank line, and splits a line longer than WIDTH into
overlapping windows (each followed by a blank line), so every chunk ends at a line boundary and
any match shorter than OVERLAP appears whole in at least one window. Standard library only.

    git log -p --text HEAD | python3 tools/security/text_windows.py | gitleaks stdin ...
"""

import sys

WIDTH = 20_000
OVERLAP = 2_000


def windows(line, width=WIDTH, overlap=OVERLAP):
    """The pieces of one line (without its newline): the line itself when it is short enough,
    otherwise windows of `width` bytes that each overlap the next by `overlap` bytes."""
    if len(line) <= width:
        return [line]
    step = width - overlap
    return [line[start : start + width] for start in range(0, len(line) - overlap, step)]


def rewrite(source, target):
    for raw in source:
        line = raw[:-1] if raw.endswith(b"\n") else raw
        for piece in windows(line):
            target.write(piece + b"\n\n")


if __name__ == "__main__":
    rewrite(sys.stdin.buffer, sys.stdout.buffer)
