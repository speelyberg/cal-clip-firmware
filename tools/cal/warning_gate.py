#!/usr/bin/env python3
"""Fail a build log on new warnings.

Fails on (a) any compiler warning in a path we own, even if it is baselined,
and (b) any warning line missing from the stock-v0.1.4 baseline.
Lines are normalized: CI timestamps and absolute workspace/home paths are stripped.
"""
import argparse
import os
import re
import sys
from pathlib import Path

WARNING = re.compile(r'(^|\s)warning( \([^)]*\))?:', re.IGNORECASE)
GH_PREFIX = re.compile(r'^(?:[^\t]*\t[^\t]*\t)?\d{4}-\d\d-\d\dT[\d:.]+Z ')
ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
COMPILER = re.compile(r'^(\S+?):\d+(?::\d+)?: warning')


def normalize(line, workspace, home):
    line = ANSI.sub('', GH_PREFIX.sub('', line)).rstrip()
    if workspace:
        line = line.replace(workspace.rstrip('/') + '/', '')
    if home:
        line = line.replace(home.rstrip('/') + '/', '~/')
    return line


def warnings(text, workspace, home):
    # ponytail: first line only; a multi-line Kconfig warning that differs only in
    # its continuation lines matches its baselined first line. Join blocks if that bites.
    return {normalize(l, workspace, home) for l in text.splitlines() if WARNING.search(l)}


def owned(line, ours):
    m = COMPILER.match(line)
    return bool(m) and any(m.group(1) == p or (p.endswith('/') and m.group(1).startswith(p)) for p in ours)


def check(found, baseline, ours):
    """Return (owned warnings, warnings missing from the baseline)."""
    return sorted(l for l in found if owned(l, ours)), sorted(found - baseline)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('logs', nargs='+', type=Path)
    ap.add_argument('--baseline', type=Path, required=True)
    ap.add_argument('--write', action='store_true', help='write the baseline from the logs instead of checking')
    ap.add_argument('--ours', action='append', default=[],
                    help='workspace-relative file, or directory ending in /, whose warnings always fail')
    ap.add_argument('--workspace', default=os.environ.get('GITHUB_WORKSPACE', ''))
    ap.add_argument('--home', default=os.environ.get('HOME', ''))
    args = ap.parse_args()
    found = set()
    for log in args.logs:
        found |= warnings(log.read_text(errors='replace'), args.workspace, args.home)
    if args.write:
        header = '# Normalized warnings from stock v0.1.4 builds (tools/cal/warning_gate.py --write).\n'
        args.baseline.write_text(header + ''.join(l + '\n' for l in sorted(found)))
        print(f'wrote {len(found)} baseline lines to {args.baseline}')
        return 0
    baseline = {l for l in args.baseline.read_text().splitlines() if l and not l.startswith('#')}
    mine, new = check(found, baseline, args.ours)
    for l in mine:
        print(f'::error::warning in our code: {l}')
    for l in new:
        print(f'::error::warning not in stock baseline: {l}')
    print(f'{len(found)} distinct warning lines, {len(mine)} in our code, {len(new)} new')
    return 1 if mine or new else 0


if __name__ == '__main__':
    sys.exit(main())
