"""Read-only comparison of launch policy with recognized live terminal footers.

A footer is observational evidence, not provider API verification. Never use
scrollback, launch argv, or a saved session as proof of current settings.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shlex
import subprocess


class ProbeError(Exception):
    pass


def run(args):
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError) as error:
        raise ProbeError from error


def settings(argv):
    """Decode the effective command emitted by the shared launch resolver."""
    result = {'model': None, 'effort': None}
    for index, argument in enumerate(argv):
        following = argv[index + 1] if index + 1 < len(argv) else ''
        if argument in ('--model', '-m'):
            result['model'] = following
        elif argument.startswith('--model='):
            result['model'] = argument.split('=', 1)[1]
        elif argument in ('--effort', '--reasoning-effort'):
            result['effort'] = following
        elif argument.startswith(('--effort=', '--reasoning-effort=')):
            result['effort'] = argument.split('=', 1)[1]
        else:
            config = following if argument in ('-c', '--config') else argument.removeprefix('--config=').removeprefix('-c')
            match = re.fullmatch(r'model_reasoning_effort\s*=\s*["\']?(\w+)["\']?', config)
            if match:
                result['effort'] = match[1]
    return result


def footer(provider, screen):
    # Capture only the current screen, and only accept complete known footer
    # shapes near its bottom. A model name mentioned in prose is not evidence.
    lines = screen.splitlines()[-5:]
    matches = []
    for line in lines:
        if provider == 'codex':
            match = re.fullmatch(r'\s+(gpt-[\w.-]+|o[134](?:-[\w.-]+)?) (none|minimal|low|medium|high|xhigh|max|ultra) · .+', line)
            if match:
                matches.append({'model': match[1], 'effort': match[2]})
        elif provider == 'grok':
            match = re.fullmatch(r'\s*╰─+ Grok (\d+(?:\.\d+)+(?:-[\w.-]+)?) \((low|medium|high|xhigh)\) · [^\n]+─╯\s*', line)
            if match:
                matches.append({'model': f'grok-{match[1]}', 'effort': match[2]})
    return matches[0] if len(matches) == 1 else None


def inspect_pane(cli, session, pane, role, provider, workspace=''):
    row = dict(session=session, pane=pane, role=role, provider=provider,
               desired={'model': None, 'effort': None}, observed=None,
               state='unknown', drift=[], source=None, reason=None)
    try:
        row['desired'] = settings(shlex.split(run([cli, 'provider-command', provider, 'fresh', 'safe', '', '', role, workspace])))
    except (ProbeError, ValueError):
        row['reason'] = 'policy_unavailable'
        return row
    if provider not in ('codex', 'grok'):
        row['reason'] = 'unsupported_live_observation'
        return row
    try:
        # Do not mistake a dead agent's leftover footer for live configuration.
        run(['agent-workspaces-pane-active', provider, session])
        screen = run(['tmux', 'capture-pane', '-p', '-t', pane])
        run(['agent-workspaces-pane-active', provider, session])
    except ProbeError:
        row['reason'] = 'agent_or_screen_unavailable'
        return row
    row['observed'] = footer(provider, screen)
    if row['observed'] is None:
        row['reason'] = 'footer_unrecognized'
        return row
    row['source'] = 'live_screen_footer'
    row['drift'] = [key for key in ('model', 'effort')
                    if row['desired'][key] is not None and row['desired'][key] != row['observed'][key]]
    row['state'] = 'drift' if row['drift'] else 'match'
    if any(value is None for value in row['desired'].values()) and not row['drift']:
        row['state'] = 'unknown'
        row['reason'] = 'desired_settings_incomplete'
    return row


def collect(workspace, cli):
    report = {'schema_version': 1, 'workspace_id': workspace,
              'checked_at': datetime.now(timezone.utc).isoformat(), 'sessions': [], 'error': None}
    try:
        panes = run(['tmux', 'list-panes', '-a', '-F',
                     '#{session_name}\t#{pane_id}\t#{@aw_role}\t#{@aw_provider}\t#{@aw_workspace_id}'])
    except ProbeError:
        report['error'] = 'tmux_unavailable'
        return report
    seen = set()
    for line in sorted(panes.splitlines()):
        fields = line.split('\t')
        if len(fields) != 5:
            continue
        session, pane, role, provider, workspace_id = fields
        if workspace_id != workspace or role == 'integration' or session in seen:
            continue
        # Workspaces owns one agent pane per session, as does the active probe.
        seen.add(session)
        report['sessions'].append(inspect_pane(cli, session, pane, role, provider or role, workspace))
    return report


def display(report):
    if report['error']:
        return 'Live configuration: unknown (' + report['error'] + ').'
    if not report['sessions']:
        return 'Live configuration: no workspace agent sessions found.'
    lines = ['Live configuration (observed from recognized screen footers):']
    for row in report['sessions']:
        desired = '/'.join(row['desired'][key] or 'unknown' for key in ('model', 'effort'))
        observed = '/'.join(row['observed'][key] for key in ('model', 'effort')) if row['observed'] else 'unknown'
        detail = ', '.join(row['drift']) if row['drift'] else row['reason']
        lines.append(f"- {row['role']} ({row['pane']}): desired {desired}; observed {observed}; {row['state']}" +
                     (f' ({detail})' if detail else ''))
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workspace')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--cli', default=str(Path(__file__).resolve().parents[1] / 'bin/agent-workspaces'))
    args = parser.parse_args()
    report = collect(args.workspace, args.cli)
    print(json.dumps(report) if args.json else display(report))


if __name__ == '__main__':
    main()
