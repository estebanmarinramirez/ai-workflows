"""Merge workspace-owned menu values while preserving unrelated JSONC text."""
import json
import os
from pathlib import Path
import sys
import tempfile


def clean_jsonc(text):
    chars = list(text)
    index = 0
    quoted = False
    while index < len(text):
        char = text[index]
        if quoted:
            if char == '\\':
                index += 2
                continue
            if char == '"': quoted = False
        elif char == '"': quoted = True
        elif text.startswith('//', index):
            end = text.find('\n', index)
            if end < 0: end = len(text)
            chars[index:end] = ' ' * (end-index)
            index = end
            continue
        elif text.startswith('/*', index):
            end = text.find('*/', index+2)
            if end < 0: raise ValueError('Unclosed JSONC comment')
            end += 2
            chars[index:end] = ' ' * (end-index)
            index = end
            continue
        index += 1
    # Replace trailing commas outside strings, retaining all source offsets.
    clean = ''.join(chars)
    index = 0
    quoted = False
    while index < len(clean):
        char = clean[index]
        if quoted:
            if char == '\\':
                index += 2
                continue
            if char == '"': quoted = False
        elif char == '"': quoted = True
        elif char == ',':
            rest = clean[index+1:].lstrip()
            if rest.startswith(('}', ']')): chars[index] = ' '
        index += 1
    return ''.join(chars)


def merge(source, destination):
    desired = json.loads(clean_jsonc(source))
    desired = {key: value for key, value in desired.items() if key == 'agent-launch' or key.startswith('agent-launch.')}
    clean = clean_jsonc(destination)
    current = json.loads(clean)
    if not isinstance(current, dict): raise ValueError('Menu must be a JSON object')
    decoder = json.JSONDecoder()
    pos = clean.index('{') + 1
    edits = []
    seen = set()
    while True:
        while clean[pos].isspace() or clean[pos] == ',': pos += 1
        if clean[pos] == '}': break
        key, pos = decoder.raw_decode(clean, pos)
        if key in seen: raise ValueError(f'Duplicate menu key: {key}')
        seen.add(key)
        while clean[pos].isspace(): pos += 1
        if clean[pos] != ':': raise ValueError('Expected colon')
        pos += 1
        while clean[pos].isspace(): pos += 1
        start = pos
        _, pos = decoder.raw_decode(clean, pos)
        if key in desired:
            edits.append((start, pos, json.dumps(desired[key], ensure_ascii=False)))
    missing = {key: value for key, value in desired.items() if key not in current}
    if missing:
        values = ',\n'.join('  '+json.dumps(key)+': '+json.dumps(value, ensure_ascii=False) for key, value in missing.items())
        edits.append((clean.index('{')+1, clean.index('{')+1, '\n'+values+(',' if current else '')+'\n'))
    for start, end, value in sorted(edits, reverse=True):
        destination = destination[:start] + value + destination[end:]
    json.loads(clean_jsonc(destination))
    return destination


def main():
    source, target = map(Path, sys.argv[1:])
    target = target.resolve()  # Preserve a dotfiles-managed symlink itself.
    mode = target.stat().st_mode & 0o7777 if target.exists() else 0o644
    result = merge(source.read_text(), target.read_text() if target.exists() else '{}\n')
    fd, temporary = tempfile.mkstemp(prefix='.menu.', dir=target.parent)
    try:
        with os.fdopen(fd, 'w') as stream: stream.write(result)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


if __name__ == '__main__':
    main()
