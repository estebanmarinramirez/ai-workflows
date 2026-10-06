"""Offline provider catalog, augmented by Codex's account-local model cache.

Catalog entries describe capabilities, not subscription entitlements. Custom
model IDs remain supported for gateways and future releases.
"""
import argparse
import json
import os
from pathlib import Path
import re

EFFORTS = ('default', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')


def config_root():
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))) / 'agent-workspaces'


def catalog(provider, root=None):
    root = root or config_root()
    if not re.fullmatch(r'[a-z0-9_-]+', provider):
        raise ValueError('Invalid provider')
    manifest = json.loads((root / 'providers' / f'{provider}.json').read_text())
    rows = {r['id']: dict(r, source='provider-manifest') for r in manifest.get('models', [])}
    if provider == 'codex':
        cache = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'models_cache.json'
        try:
            data = json.loads(cache.read_text())
            for model in data['models']:
                name = model.get('slug')
                if not isinstance(name, str) or not name or model.get('visibility') != 'list':
                    continue
                efforts = [r['effort'] for r in model.get('supported_reasoning_levels', [])
                           if r.get('effort') in EFFORTS]
                if efforts:
                    rows[name] = dict(id=name, efforts=efforts, source='codex-cache')
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass  # Corrupt/unavailable cache never prevents offline selection.
    elif provider == 'grok':
        cache = Path(os.environ.get('GROK_HOME', str(Path.home() / '.grok'))) / 'models_cache.json'
        try:
            data = json.loads(cache.read_text())
            for name, model in data['models'].items():
                info = model['info']
                efforts = [r['value'] for r in info.get('reasoning_efforts', []) if r.get('value') in EFFORTS]
                if info.get('supports_reasoning_effort') is False:
                    efforts = ['default']
                if efforts:
                    # Cache also holds endpoint/credential fields. Never expose them.
                    rows[name] = dict(id=name, efforts=efforts, source='grok-cache')
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
    return list(rows.values())


def efforts_for(provider, model, root=None):
    for row in catalog(provider, root):
        if row['id'] == model:
            return ['default'] + [e for e in row['efforts'] if e != 'default']
    # Custom deployments have no verified per-model capability metadata.
    return ['default', 'low', 'medium', 'high']


def validate(provider, model, effort, root=None):
    if not model or not model.strip() or any(ord(c) < 32 for c in model):
        raise ValueError('Choose a nonempty model name without control characters')
    allowed = efforts_for(provider, model, root)
    if effort not in allowed:
        raise ValueError(f'{provider}/{model} supports: {", ".join(allowed)}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('provider')
    parser.add_argument('--efforts', metavar='MODEL')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    try:
        result = efforts_for(args.provider, args.efforts) if args.efforts else catalog(args.provider)
        if args.json:
            print(json.dumps(result))
        else:
            print('\n'.join(result if args.efforts else [r['id'] for r in result]))
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f'agent-workspaces models: {error}\n')


if __name__ == '__main__':
    main()
