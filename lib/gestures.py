"""Gesture actions and desktop transport; recognition is kept in gesture_camera.py."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid


class GestureError(Exception):
    pass


def command(args, text=None):
    try:
        return subprocess.check_output(args, input=text, text=True, stderr=subprocess.PIPE, timeout=4).strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise GestureError(f'{args[0]} {args[1] if len(args) > 1 else ""} failed') from error


@dataclass(frozen=True)
class Target:
    address: str
    session: str
    pane: str
    role: str
    provider: str
    workspace: str
    class_name: str
    pane_pid: str
    agent_pid: str = ''


class Hold:
    """One event per held pose; require neutral before the next action."""
    def __init__(self, seconds=.8, send_seconds=1.3, release_seconds=.35):
        self.seconds, self.send_seconds, self.release_seconds = seconds, send_seconds, release_seconds
        self.key = None
        self.since = 0.
        self.latched = False
        self.neutral_since = None

    def reset(self):
        self.key = None
        self.latched = True
        self.neutral_since = None

    def update(self, key, now):
        if key is None:
            if self.neutral_since is None:
                self.neutral_since = now
            if now - self.neutral_since >= self.release_seconds:
                self.latched = False
            # Brief classifier dropouts should not restart every hold.
            if now - self.neutral_since > .15:
                self.key = None
            return None
        if self.neutral_since is not None and now - self.neutral_since > .15:
            self.key = None
        self.neutral_since = None
        if self.latched:
            return None
        if key != self.key:
            self.key, self.since = key, now
            return None
        delay = self.send_seconds if key == 'send' else self.seconds
        if now - self.since < delay:
            return None
        self.latched = True
        return key


def composer_state(screen):
    # Inspect the whole visible screen: first-run questions can sit above the
    # footer, and both Codex menus and composers use the same prompt glyph.
    if re.search(r'(?i)(do you trust|trust (?:the contents|this (?:folder|directory))|accessing workspace:|yes, i trust|is this a project you created)', screen):
        return 'trust-required'
    if re.search(r'(?i)(do you want to (?:allow|proceed)|approve this|allow once|yes, proceed|esc to cancel|enter to confirm|press enter to continue)', screen):
        return 'confirmation-required'
    lines = screen.splitlines()[-12:]
    if any(re.match(r'^\s*[│┃]?\s*[›❯]\s*\d+[.)]\s', line) for line in lines):
        return 'confirmation-required'
    return 'ready' if any(re.match(r'^\s*[│┃]?\s*[›❯](?:\s|$)', line) for line in lines) else 'no-composer'


def composer(screen):
    return composer_state(screen) == 'ready'


def clean_text(text):
    # Dictation is one composer message: embedded newlines/control characters
    # must not become terminal keystrokes or execute another command.
    return ' '.join(''.join(c if c.isprintable() else ' ' for c in text).split())


CONTROL_AREAS = {'laptop': (.20, .23, .80, .77), 'desktop': (.15, .15, .85, .85)}
POINTING_GAP = .10  # Inactive central bands, as a fraction of the control box.


def pointing_active(x, y, profile='laptop'):
    left, top, right, bottom = CONTROL_AREAS[profile]
    nx, ny = (x - left) / (right - left), (y - top) / (bottom - top)
    return (0 <= nx <= 1 and 0 <= ny <= 1
            and abs(nx - .5) > POINTING_GAP / 2
            and abs(ny - .5) > POINTING_GAP / 2)


def pointing_box(x, y, profile='laptop'):
    if not pointing_active(x, y, profile):
        return None
    left, top, right, bottom = CONTROL_AREAS[profile]
    return ('bottom' if y > (top + bottom) / 2 else 'top') + '-' + ('right' if x > (left + right) / 2 else 'left')


def pointing_position(x, y, profile='laptop'):
    left, top, right, bottom = CONTROL_AREAS[profile]
    # Keep screen-edge targets inside the usual outer window gaps.
    return tuple(.02 + .96 * min(1., max(0., (value - low) / (high - low)))
                 for value, low, high in ((x, left, right), (y, top, bottom)))


def accepted_gesture(name, score, profile='laptop'):
    threshold = .6 if profile == 'laptop' and name in {'Pointing_Up', 'Open_Palm', 'Closed_Fist'} else .8
    return name if score >= threshold else None


class Desktop:
    def __init__(self, profile='laptop'):
        self.profile = profile

    def windows(self):
        monitors = json.loads(command(['hyprctl', 'monitors', '-j']))
        monitor = next((m for m in monitors if m.get('focused')), None)
        if not monitor:
            return [], None
        current = monitor['activeWorkspace']['id']
        clients = json.loads(command(['hyprctl', 'clients', '-j']))
        return [c for c in clients if c.get('workspace', {}).get('id') == current and c.get('monitor') == monitor['id']
                and c.get('mapped', True) and not c.get('hidden', False)
                and not c.get('title', '').startswith('Workspace hand controls -')], monitor

    def candidate(self, x, y):
        if not pointing_active(x, y, self.profile):
            return None
        windows, monitor = self.windows()
        if not monitor:
            return None
        x, y = pointing_position(x, y, self.profile)
        scale = monitor.get('scale', 1)
        width, height = monitor['width'] / scale, monitor['height'] / scale
        if monitor.get('transform', 0) % 2:
            width, height = height, width
        px, py = monitor['x'] + x * width, monitor['y'] + y * height
        hit = [c for c in windows if c['at'][0] <= px <= c['at'][0] + c['size'][0]
               and c['at'][1] <= py <= c['at'][1] + c['size'][1]]
        if len(hit) != 1:
            return None
        return hit[0]

    def target(self, window):
        if not window.get('class', '').startswith('org.omarchy.agentworkspaces.'):
            return Target(window['address'], '', '', window.get('class') or 'window', '', '', window.get('class', ''), '')
        rows = command(['tmux', 'list-sessions', '-F', '#{session_name}\t#{@aw_role}\t#{@aw_provider}\t#{@aw_workspace_id}\t#{@aw_class_prefix}'])
        for row in rows.splitlines():
            fields = row.split('\t')
            if len(fields) != 5:
                continue
            session, role, provider, workspace, prefix = fields
            if f'{prefix}.{role}' != window['class']:
                continue
            panes = command(['tmux', 'list-panes', '-t', session, '-F', '#{pane_id}\t#{pane_pid}']).splitlines()
            if len(panes) != 1:
                raise GestureError('Select a workspace with one agent pane per window')
            pane, pid = panes[0].split('\t')
            provider = provider or role
            return Target(window['address'], session, pane, role, provider, workspace, window['class'], pid,
                          self.agent_pid(pid, provider))
        raise GestureError('Window has no managed agent session')

    def agent_pid(self, pane_pid, provider):
        tree = command(['pstree', '-p', pane_pid])
        matches = set(re.findall(r'\b' + re.escape(provider) + r'\((\d+)\)', tree))
        if len(matches) != 1:
            raise GestureError('Agent process is not uniquely identifiable; wait for startup and select again')
        return matches.pop()

    def focus(self, target):
        try:
            command(['hyprctl', 'dispatch', f'hl.dsp.focus({{ window = "address:{target.address}" }})'])
        except GestureError:
            command(['hyprctl', 'dispatch', 'focuswindow', f'address:{target.address}'])

    def validate(self, target, require_focus=True):
        if not target.pane:
            raise GestureError('This window supports focus only. Dictation requires a managed agent workspace window.')
        windows = json.loads(command(['hyprctl', 'clients', '-j']))
        if not any(c['address'] == target.address and c.get('class') == target.class_name for c in windows):
            raise GestureError('Selected window closed; select a window again')
        metadata = command(['tmux', 'display-message', '-p', '-t', target.pane,
                            '#{session_name}\t#{pane_pid}\t#{@aw_provider}'])
        if metadata != f'{target.session}\t{target.pane_pid}\t{target.provider}':
            raise GestureError('Selected agent changed; select it again')
        if target.agent_pid and self.agent_pid(target.pane_pid, target.provider) != target.agent_pid:
            raise GestureError('Selected agent restarted; select it again')
        command(['omarchy-agent-pane-active', target.role, target.session])
        if require_focus:
            active = json.loads(command(['hyprctl', 'activewindow', '-j']))
            if active.get('address') != target.address:
                raise GestureError('Point at the selected window to return to your draft')
        state = composer_state(command(['tmux', 'capture-pane', '-p', '-t', target.pane]))
        if state == 'trust-required':
            raise GestureError('First-run trust prompt: finish setup with the keyboard before dictating')
        if state == 'confirmation-required':
            raise GestureError('Agent is waiting for confirmation; gesture sending is disabled')
        if state != 'ready':
            raise GestureError('Selected agent has no recognized message composer')

    def readiness(self, target):
        try:
            self.validate(target, require_focus=False)
            return 'ready'
        except GestureError as error:
            return str(error)

    def status(self):
        status = command(['voxtype', 'status'])
        if status not in {'idle', 'recording', 'transcribing'}:
            raise GestureError('Voxtype is not ready; start its daemon first')
        return status

    def start(self, target, output):
        self.validate(target)
        if self.status() != 'idle':
            raise GestureError('Dictation is already in use')
        command(['voxtype', 'record', 'start', f'--file={output}', '--no-auto-submit', '--no-smart-auto-submit'])

    def stop(self):
        command(['voxtype', 'record', 'stop'])

    def cancel(self):
        command(['voxtype', 'record', 'cancel'])

    def paste(self, target, text):
        self.validate(target)
        buffer = 'aw-gesture-' + uuid.uuid4().hex
        command(['tmux', 'load-buffer', '-b', buffer, '-'], text)
        try:
            command(['tmux', 'paste-buffer', '-p', '-b', buffer, '-t', target.pane])
        finally:
            try:
                command(['tmux', 'delete-buffer', '-b', buffer])
            except GestureError:
                pass  # A cleanup error must never trigger a second paste.

    def send(self, target):
        self.validate(target)
        command(['tmux', 'send-keys', '-t', target.pane, 'Enter'])


class Controller:
    def __init__(self, desktop, directory, report=print):
        self.desktop, self.directory, self.report = desktop, Path(directory), report
        self.target = None
        self.state = 'idle'
        self.output = None
        self.started = 0.
        self.last_poll = 0.
        self.message = ''
        self.hold = Hold()

    def observe(self, gesture, candidate, now):
        """Route a recognized pose through the same state machine in tests/live."""
        key = None
        if gesture == 'Pointing_Up' and candidate:
            key = ('select', candidate['address'])
        elif gesture == 'Open_Palm' and self.state == 'idle':
            key = 'start'
        elif gesture == 'Closed_Fist' and self.state == 'recording':
            key = 'stop'
        elif gesture == 'Thumb_Up' and self.state == 'draft':
            key = 'send'
        elif gesture not in (None, 'None'):
            key = ('inactive', gesture)
        event = self.hold.update(key, now)
        if isinstance(event, tuple):
            if event[0] == 'select':
                self.select(candidate)
        elif event:
            self.action(event, now)

    def say(self, message):
        if message != self.message:
            self.message = message
            self.report(message)

    def select(self, window):
        target = self.desktop.target(window)
        if self.state != 'idle' and target != self.target:
            raise GestureError('Finish this dictation before selecting another window')
        self.desktop.focus(target)
        self.target = target
        readiness = self.desktop.readiness(target)
        if readiness != 'ready':
            self.say(f'Selected {target.role}. {readiness}')
            return
        self.say(f'Selected {target.role}. Open palm to dictate.' if target.pane else
                 f'Selected {target.role}. Focus only; open an agent workspace grid for dictation.')

    def action(self, action, now):
        if not self.target:
            raise GestureError('Point and hold to select a workspace window first')
        if action == 'start' and self.state == 'idle':
            fd, name = tempfile.mkstemp(prefix='dictation-', suffix='.txt', dir=self.directory)
            os.close(fd)
            self.output = Path(name)
            try:
                self.desktop.start(self.target, self.output)
            except Exception:
                self.output.unlink(missing_ok=True)
                self.output = None
                raise
            self.state, self.started = 'recording', now
            self.say(f'Recording for {self.target.role}. Closed fist to stop.')
        elif action == 'stop' and self.state == 'recording':
            self.desktop.stop()
            self.state, self.started = 'transcribing', now
            self.say('Transcribing. Wait for the draft before sending.')
        elif action == 'send' and self.state == 'draft':
            # The transport might fail after Enter was delivered. Never offer
            # automatic retry of an ambiguous send.
            self.state = 'review'
            try:
                self.desktop.send(self.target)
            except GestureError as error:
                self.say('Send could not be confirmed. Check the pane manually; restart controls to reset.')
                raise error
            self.state = 'idle'
            self.say('Message sent. Open palm for another dictation.')
        self.hold.reset()

    def poll(self, now):
        if self.state not in {'recording', 'transcribing'} or now - self.last_poll < .3:
            return
        self.last_poll = now
        if now - self.started > 125:
            self.close()
            self.say('Dictation timed out. Select a window to try again.')
            return
        status = self.desktop.status()
        if self.state == 'recording' and status == 'transcribing':
            self.state, self.started = 'transcribing', now
        if status == 'idle' and self.output and self.output.stat().st_size:
            text = clean_text(self.output.read_text())
            if text:
                self.state = 'review'
                try:
                    self.desktop.paste(self.target, text)
                except GestureError:
                    self.say('Paste could not be confirmed. Check the pane manually; restart controls to reset.')
                    raise
                self.state = 'draft'
                self.say(f'Draft in {self.target.role}. Lower your hand, then hold thumbs-up to send.')
            else:
                self.state = 'idle'
                self.say('No speech recognized. Open palm to try again.')
            self.output.unlink(missing_ok=True)
            self.output = None
            self.hold.reset()
        elif status == 'idle' and now - self.started > 3:
            self.output.unlink(missing_ok=True)
            self.output = None
            self.state = 'idle'
            self.hold.reset()
            self.say('No speech recognized. Lower your hand, then open palm to try again.')

    def close(self):
        if self.state in {'recording', 'transcribing'}:
            try:
                self.desktop.cancel()
            except GestureError:
                pass
        if self.output:
            self.output.unlink(missing_ok=True)
        self.output = None
        self.state = 'idle'
