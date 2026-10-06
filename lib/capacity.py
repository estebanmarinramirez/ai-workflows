"""Provider identity and conservative account-pool telemetry, shared by UI/router."""
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path

TERMINAL = {'completed', 'draft_pr', 'ready_for_pr', 'cancelled', 'archived', 'superseded'}


def read_object(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result if result.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def provider_for_slot(root, slot, task=None, config=None):
    active = read_object(Path(root) / '.coordination/agents.json').get('active', {})
    manifest = read_object(Path(root) / 'workspace.json')
    roster = {row['role']: row['provider'] for row in manifest.get('agents', [])
              if isinstance(row, dict) and 'role' in row and 'provider' in row}
    # Stable slots retain their names after a provider switch.
    provider = (active.get(slot, {}).get('provider') or
                (task or {}).get('roles', {}).get(slot, {}).get('provider') or roster.get(slot))
    if not provider and slot == 'orchestrator':
        provider = (config or {}).get('orchestrator_provider', 'codex')
    if not provider:
        configured = (config or {}).get('providers', ['claude', 'codex', 'grok'])
        provider = next((p for p in configured if slot == p or slot.startswith(p + '-')), None)
    return provider


def pool_id(provider, policy):
    return policy.get('account_pools', {}).get(provider, provider)


def read_capacity(provider, root, now, ttl):
    row = dict(provider=provider, state='unknown', used=None, remaining=None, resets_at=None, reason='missing telemetry')
    record = read_object(Path(root) / f'{provider}.json')
    if record.get('id') != provider or record.get('ready') is not True:
        return row
    updated = timestamp(record.get('updatedAt'))
    if updated is None or not -30 <= (now - updated).total_seconds() <= ttl:
        row['reason'] = 'stale or invalid telemetry timestamp'
        return row
    limits = record.get('limits')
    if not isinstance(limits, list) or not limits:
        row['reason'] = 'no authoritative usage limits'
        return row
    values, resets = [], []
    for limit in limits:
        if not isinstance(limit, dict):
            row['reason'] = 'invalid usage limit'
            return row
        percent = limit.get('percent')
        if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not math.isfinite(percent) or not 0 <= percent <= 1:
            row['reason'] = 'invalid usage percentage'
            return row
        reset = timestamp(limit.get('resetsAt'))
        if limit.get('resetsAt') and (reset is None or reset <= now):
            row['reason'] = 'limit reset requires a fresh provider observation'
            return row
        values.append(percent)
        if reset: resets.append(reset)
    used = max(values)
    return dict(row, state='exhausted' if used >= 1 else 'available', used=used, remaining=round(1-used, 6),
                updated_at=updated.isoformat(), resets_at=min(resets).isoformat() if resets else None, reason='most constrained provider limit')


def capacities(config, usage_root=None, now=None):
    now = now or datetime.now(timezone.utc)
    usage_root = usage_root or Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'omarchy/agents/usage'
    policy = config.get('routing', {})
    ttl = policy.get('telemetry_ttl_seconds', 300)
    providers = {p: read_capacity(p, usage_root, now, ttl) for p in config.get('providers', [])}
    pools = {}
    for provider, row in providers.items():
        key = pool_id(provider, policy)
        pool = pools.setdefault(key, dict(id=key, providers=[], state='unknown', remaining=None, used=None, incomplete=False, resets_at=None, updated_at=None, reason='shared account pool'))
        pool['providers'].append(provider)
        if row['state'] == 'unknown':
            pool.update(incomplete=True, reason=f'{provider}: {row["reason"]}')
        else:
            # Partial evidence cannot authorize spending, but it still supplies
            # an upper bound on remaining quota that can forbid spending.
            pool['used'] = max(pool['used'] or 0, row['used'])
            pool['remaining'] = round(1 - pool['used'], 6)
        pool['state'] = ('exhausted' if pool['remaining'] == 0 else
                         'unknown' if pool['incomplete'] else 'available')
        if row['resets_at']:
            pool['resets_at'] = min(filter(None, [pool['resets_at'], row['resets_at']]))
        if row.get('updated_at'):
            pool['updated_at'] = min(filter(None, [pool['updated_at'], row['updated_at']]))
    return providers, pools
