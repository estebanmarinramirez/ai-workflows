"""Local webcam gesture recognition for Agent Workspaces."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

from gestures import Controller, Desktop, GestureError, CONTROL_AREAS, POINTING_GAP, accepted_gesture, pointing_box


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--camera', default='/dev/video0')
    parser.add_argument('--dry-run', action='store_true', help='Recognize gestures without focusing, recording, pasting or sending')
    parser.add_argument('--preview', action='store_true', help='Show a local mirrored camera preview; Escape closes it')
    parser.add_argument('--profile', choices=CONTROL_AREAS, default='laptop', help='Laptop uses a smaller, higher pointing area for close camera use')
    parser.add_argument('--seconds', type=float, default=1800, help='Stop after this many seconds (default: 30 minutes; 0 disables cutoff)')
    args = parser.parse_args()
    if args.seconds < 0:
        parser.error('--seconds cannot be negative')
    import cv2
    import mediapipe as mp

    runtime = Path(os.environ.get('XDG_RUNTIME_DIR', tempfile.gettempdir()))
    lock = (runtime / f'agent-workspaces-gestures-{os.getuid()}.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.exit(1, 'Workspace gesture controller is already running.\n')
    running = True
    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    status_path = runtime / 'agent-workspaces-gestures.json'
    status = {'pid': os.getpid(), 'mode': 'preview' if args.dry_run else 'live', 'profile': args.profile}
    def save_status():
        temporary = status_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(status))
        temporary.replace(status_path)
    def report(message):
        print(message, flush=True)
        status['message'] = message
        save_status()
        if not args.dry_run:
            try:
                subprocess.run(['notify-send', '-a', 'Workspaces', '-t', '3000', 'Hand controls', message], check=False, timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass  # Desktop notifications must not block camera shutdown.

    desktop = Desktop(args.profile)
    tracking_confidence = .5 if args.profile == 'laptop' else .65
    options = mp.tasks.vision.GestureRecognizerOptions(
        base_options=mp.tasks.BaseOptions(model_asset_path=args.model),
        running_mode=mp.tasks.vision.RunningMode.VIDEO, num_hands=1,
        min_hand_detection_confidence=tracking_confidence, min_hand_presence_confidence=tracking_confidence,
        min_tracking_confidence=tracking_confidence)
    camera = cv2.VideoCapture(args.camera, cv2.CAP_V4L2)
    if not camera.isOpened():
        camera.release()
        status_path.unlink(missing_ok=True)
        lock.close()
        parser.exit(1, 'Cannot open camera. Check its privacy shutter and device availability.\n')
    camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    camera.set(cv2.CAP_PROP_FPS, 20)
    started = time.monotonic()
    timestamp = 0
    candidate = None
    last_dry_gesture = None
    last_status = 0.
    frames = 0
    visible_windows = []
    try:
        with tempfile.TemporaryDirectory(prefix='aw-dictation-', dir=runtime) as directory:
            controller = Controller(desktop, directory, report)
            try:
                with mp.tasks.vision.GestureRecognizer.create_from_options(options) as recognizer:
                    report('Camera preview: desktop actions disabled.' if args.dry_run else 'Camera on. Point and hold to select; palm records; fist stops; thumbs-up sends. Lower your hand between gestures.')
                    failures = 0
                    while running and (not args.seconds or time.monotonic() - started < args.seconds):
                        good, frame = camera.read()
                        if not good:
                            failures += 1
                            if failures > 15:
                                raise GestureError('Camera stopped delivering frames')
                            time.sleep(.05)
                            continue
                        failures = 0
                        frames += 1
                        frame = cv2.flip(frame, 1)
                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        now = time.monotonic()
                        timestamp = max(timestamp + 1, int(now * 1000))
                        result = recognizer.recognize_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), timestamp)
                        gesture = None
                        category = result.gestures[0][0] if result.gestures and result.gestures[0] else None
                        if category:
                            gesture = accepted_gesture(category.category_name, category.score, args.profile)
                        tip = None
                        hovered_box = None
                        if result.hand_landmarks:
                            tip = result.hand_landmarks[0][4 if category and category.category_name == 'Thumb_Up' else 8]
                            hovered_box = pointing_box(tip.x, tip.y, args.profile)
                        if now - last_status >= 1:
                            try:
                                visible_windows, _ = desktop.windows()
                                desktop_error = ''
                            except GestureError as error:
                                visible_windows = []
                                desktop_error = str(error)
                            status.update(updated_at=time.time(), frames=frames,
                                          fps=round(frames / max(.01, now - started), 1),
                                          hands=len(result.hand_landmarks),
                                          gesture=category.category_name if category else None,
                                          confidence=round(category.score, 3) if category else 0,
                                          brightness=round(float(rgb.mean()), 1),
                                          selectable_windows=len(visible_windows),
                                          desktop_error=desktop_error, state=controller.state,
                                          selected_role=controller.target.role if controller.target else None,
                                          selected_workspace=controller.target.workspace if controller.target else None,
                                          selected_readiness=desktop.readiness(controller.target) if controller.target else 'Select a window first',
                                          hovered_window=candidate.get('class') if candidate else None,
                                          pointing_box=hovered_box,
                                          shutdown_in_seconds=max(0, round(args.seconds - (now - started))) if args.seconds else None)
                            save_status()
                            last_status = now
                        if args.dry_run:
                            if gesture != last_dry_gesture:
                                print(f'Recognized: {gesture or "no hand"}', flush=True)
                                last_dry_gesture = gesture
                        else:
                            try:
                                controller.poll(now)
                                candidate = None
                                if gesture == 'Pointing_Up' and result.hand_landmarks:
                                    index_tip = result.hand_landmarks[0][8]
                                    candidate = desktop.candidate(index_tip.x, index_tip.y)
                                    if candidate is None and not visible_windows:
                                        controller.say('No selectable windows on this desktop.')
                                controller.observe(gesture, candidate, now)
                            except GestureError as error:
                                controller.say(str(error))
                        if args.preview:
                            height, width = frame.shape[:2]
                            left, top, right, bottom = CONTROL_AREAS[args.profile]
                            start = (int(left * width), int(top * height))
                            end = (int(right * width), int(bottom * height))
                            cv2.rectangle(frame, start, end, (255, 200, 80), 2)
                            mid_x, mid_y = (left + right) / 2, (top + bottom) / 2
                            gap_x, gap_y = (right - left) * POINTING_GAP / 2, (bottom - top) * POINTING_GAP / 2
                            cv2.rectangle(frame, (int((mid_x - gap_x) * width), start[1]),
                                          (int((mid_x + gap_x) * width), end[1]), (255, 200, 80), -1)
                            cv2.rectangle(frame, (start[0], int((mid_y - gap_y) * height)),
                                          (end[0], int((mid_y + gap_y) * height)), (255, 200, 80), -1)
                            for row, y1, y2 in [('top', top, mid_y - gap_y), ('bottom', mid_y + gap_y, bottom)]:
                                for col, x1, x2 in [('left', left, mid_x - gap_x), ('right', mid_x + gap_x, right)]:
                                    name = f'{row}-{col}'
                                    box_start, box_end = (int(x1 * width), int(y1 * height)), (int(x2 * width), int(y2 * height))
                                    highlighted = name == hovered_box
                                    color = (80, 255, 120) if highlighted else (255, 200, 80)
                                    cv2.rectangle(frame, box_start, box_end, color, 4 if highlighted else 1)
                                    cv2.putText(frame, name, (box_start[0] + 6, box_start[1] + 20), cv2.FONT_HERSHEY_SIMPLEX, .45, color, 1)
                            if tip:
                                cv2.circle(frame, (int(tip.x * width), int(tip.y * height)), 8, (80, 255, 120) if hovered_box else (80, 200, 255), 2)
                            for hand in result.hand_landmarks:
                                for point in hand:
                                    cv2.circle(frame, (int(point.x * frame.shape[1]), int(point.y * frame.shape[0])), 3, (80, 240, 120), -1)
                            label = f'{category.category_name} {category.score:.0%}' if category else 'No hand detected'
                            cv2.putText(frame, label, (15, 35), cv2.FONT_HERSHEY_SIMPLEX, .8, (80, 240, 120), 2)
                            cv2.putText(frame, 'DRY RUN - no desktop actions' if args.dry_run else controller.state.upper(), (15, 65), cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 1)
                            cv2.putText(frame, f'{len(visible_windows)} selectable windows on this desktop', (15, 95), cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 1)
                            selected = controller.target.role if controller.target else 'none'
                            cv2.putText(frame, f'Selected: {selected}'[:65], (15, height - 110), cv2.FONT_HERSHEY_SIMPLEX, .5, (255, 255, 255), 1)
                            readiness = status.get('selected_readiness', 'Select a window first')
                            cv2.putText(frame, readiness[:75], (15, height - 88), cv2.FONT_HERSHEY_SIMPLEX, .45, (80, 200, 255), 1)
                            if args.seconds:
                                remaining = max(0, int(args.seconds - (now - started)))
                                cv2.putText(frame, f'Camera off in {remaining // 60:02d}:{remaining % 60:02d}', (15, height - 65), cv2.FONT_HERSHEY_SIMPLEX, .55, (255, 255, 255), 1)
                            cv2.putText(frame, f'{args.profile}: move index tip inside the blue box', (15, height - 40), cv2.FONT_HERSHEY_SIMPLEX, .55, (255, 255, 255), 1)
                            if any(p.y > .97 or p.x < .02 or p.x > .98 for hand in result.hand_landmarks for p in hand):
                                cv2.putText(frame, 'Hand near camera edge - keep palm and fingers visible', (15, height - 15), cv2.FONT_HERSHEY_SIMPLEX, .5, (80, 200, 255), 1)
                            cv2.imshow('Workspace hand controls - Esc to close', frame)
                            if cv2.waitKey(1) & 0xff == 27:
                                break
            finally:
                controller.close()
    finally:
        camera.release()
        if args.preview:
            cv2.destroyAllWindows()
        status_path.unlink(missing_ok=True)
        lock.close()
        print('Camera off.', flush=True)


if __name__ == '__main__':
    main()
