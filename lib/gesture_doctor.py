"""Read-only checks for gesture recipients; never opens camera or microphone."""
import argparse
import json

from gestures import Desktop, GestureError, command


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--all', action='store_true', help='Include managed windows on other desktops')
    args = parser.parse_args()
    desktop = Desktop()
    visible, monitor = desktop.windows()
    windows = json.loads(command(['hyprctl', 'clients', '-j'])) if args.all else visible
    records = []
    for window in windows:
        if args.all and not window.get('class', '').startswith('org.omarchy.agentworkspaces.'):
            continue
        record = {'desktop': window['workspace']['id'], 'class': window.get('class', '')}
        try:
            target = desktop.target(window)
            record.update(role=target.role, pane=target.pane or None, readiness=desktop.readiness(target))
        except GestureError as error:
            record['readiness'] = str(error)
        records.append(record)
    try:
        dictation = desktop.status()
    except GestureError as error:
        dictation = str(error)
    print(json.dumps({'active_desktop': monitor['activeWorkspace']['id'] if monitor else None,
                      'dictation': dictation, 'windows': records}, indent=2))


if __name__ == '__main__':
    main()
