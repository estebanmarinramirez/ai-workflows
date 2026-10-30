"""Provider identity and conservative account-pool telemetry, shared by UI/router."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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


def credit_row(state='unknown', reason='no paid-allowance evidence', **extra):
    return dict(dict(state=state, usable=state in ('available', 'unlimited'), unit=None, balance=None, source=None, observed_at=None, reason=reason), **extra)


def normalize_credits(raw, now, ttl, minimum=0):
    """Provider-confirmed paid allowance. Anything missing, stale or malformed is unknown, never zero or positive.

    Balances are provider- and unit-specific and are never converted to a percentage or pooled across providers.
    ordinaryUsageAllowed=false denies the whole account; spendControlReached=true denies only paid usage;
    paid usage additionally needs both flags confirmed (true / false), because absent permission is unknown.
    """
    if not isinstance(raw, dict):
        return credit_row()
    observed = timestamp(raw.get('observedAt'))
    if observed is None or not -30 <= (now - observed).total_seconds() <= ttl:
        return credit_row(reason='stale or invalid credit observation')
    if raw.get('ordinaryUsageAllowed') is False:  # a fresh hard denial needs no other field
        return credit_row('denied', 'provider denies ordinary usage', account_denied=True, observed_at=observed.isoformat())
    unit, source = raw.get('unit'), raw.get('source')
    if not isinstance(unit, str) or not unit.strip() or not isinstance(source, str) or not source.strip():
        return credit_row(reason='credit evidence lacks a unit or source')
    base = dict(unit=unit.strip()[:16], source=source.strip()[:64], observed_at=observed.isoformat())
    try:
        floor = Decimal(str(minimum))
    except InvalidOperation:
        floor = Decimal(0)
    # The configured minimum is a property of the evidence, kept whatever the allowance state, so that
    # merging aliases can still apply it to a finite balance reported by another alias.
    base['minimum'] = format(max(Decimal(0), floor) if floor.is_finite() else Decimal(0), 'f')
    flags = {key: raw.get(key) for key in ('hasCredits', 'unlimited', 'ordinaryUsageAllowed', 'spendControlReached')}
    if any(value is not None and not isinstance(value, bool) for value in flags.values()):
        return credit_row(reason='malformed credit permission flags', **base)
    if flags['ordinaryUsageAllowed'] is False:
        return credit_row('denied', 'provider denies ordinary usage', account_denied=True, **base)
    if flags['spendControlReached'] is True:
        return credit_row('denied', 'provider spend control reached', **base)
    if flags['ordinaryUsageAllowed'] is not True or flags['spendControlReached'] is not False:
        return credit_row(reason='paid-usage permission or spend control is unconfirmed', **base)
    if flags['unlimited'] is True:
        return credit_row('unlimited', 'provider-confirmed unlimited allowance', **base)
    if flags['hasCredits'] is None or raw.get('balance') is None:
        return credit_row(reason='credit balance is incomplete', **base)
    balance = raw['balance']
    if isinstance(balance, bool) or not isinstance(balance, (str, int, float)):
        return credit_row(reason='malformed credit balance', **base)
    try:
        value = Decimal(balance.strip() if isinstance(balance, str) else repr(balance))
    except InvalidOperation:
        return credit_row(reason='malformed credit balance', **base)
    if not value.is_finite():
        return credit_row(reason='nonfinite credit balance', **base)
    base['balance'] = format(value, 'f')
    floor = Decimal(base['minimum'])
    if flags['hasCredits'] is False or value <= floor:
        return credit_row('none', 'no usable credit balance above the configured minimum', **base)
    return credit_row('available', 'fresh positive provider-confirmed credits', **base)


def read_credits(provider, root, record, now, ttl, minimum=0):
    """The owned sidecar wins over a native record's optional credits object."""
    sidecar = Path(root).parent / 'agent-workspaces-credits' / f'{provider}.json'
    raw = read_object(sidecar)
    present = sidecar.exists() or 'credits' in record
    if raw.get('provider', provider) != provider:
        return credit_row(reason='credit evidence belongs to another provider', evidence_present=True)
    row = normalize_credits(raw or record.get('credits'), now, ttl, minimum)
    row['evidence_present'] = present
    return row


def ordinary_allowance(pool, reserve, paid=True):
    """(allowed, via) for ordinary managed work. Credits are not subject to the percentage reserve, are never
    available to experiments or boosts (paid=False) and never repair unknown subscription evidence,
    including an account alias whose own limits are unobserved."""
    credits = pool.get('credits') or {}
    if account_denied(pool) or pool.get('state') == 'unknown' or pool.get('incomplete') or pool.get('remaining') is None:
        return False, None
    if pool['remaining'] > reserve:
        return True, 'subscription'
    if paid and credits.get('usable'):
        return True, 'unlimited' if credits['state'] == 'unlimited' else 'credits'
    return False, None


def subscription_row(provider, record, now, ttl):
    row = dict(provider=provider, state='unknown', used=None, remaining=None, resets_at=None, reason='missing telemetry')
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


def newest_subscription(native, overlay):
    """An owned overlay observed by the read-only collector competes with the native record: the newer
    authoritative observation wins and a tie keeps the more constrained one, so an older permissive
    cache can never authorize work that a newer observation forbids."""
    if overlay is None or overlay['state'] == 'unknown':
        return native
    if native['state'] == 'unknown':
        return overlay
    newer = (timestamp(overlay['updated_at']), timestamp(native['updated_at']))
    if newer[0] != newer[1]:
        return overlay if newer[0] > newer[1] else native
    return overlay if overlay['used'] >= native['used'] else native


def read_capacity(provider, root, now, ttl, minimum=0):
    record = read_object(Path(root) / f'{provider}.json')
    sidecar = Path(root).parent / 'agent-workspaces-credits' / f'{provider}.json'
    raw = read_object(sidecar)
    overlay = None
    if raw.get('provider', provider) == provider and isinstance(raw.get('limits'), list):
        overlay = subscription_row(provider, dict(id=provider, ready=True, updatedAt=raw.get('observedAt'), limits=raw['limits']), now, ttl)
    row = newest_subscription(subscription_row(provider, record, now, ttl), overlay)
    return dict(row, credits=read_credits(provider, root, record, now, ttl, minimum))


def merge_credits(current, observed):
    """Aliases of one account share one balance. Take the most restrictive view and enforce the strictest
    configured minimum against the lowest balance; never add balances together or convert units."""
    if current is None:
        return dict(observed)
    rank = {'denied': 0, 'unknown': 1, 'none': 2, 'available': 3, 'unlimited': 4}
    merged = dict(min(current, observed, key=lambda c: rank[c['state']]))
    denied = bool(current.get('account_denied') or observed.get('account_denied'))
    paid = ('available', 'unlimited')
    if denied:
        merged.update(state='denied', usable=False, account_denied=True)
    elif current['state'] in paid and observed['state'] in paid:
        if current['unit'] != observed['unit']:
            merged.update(state='unknown', usable=False, reason='aliases report incompatible credit units')
        else:
            floor = max(Decimal(current.get('minimum', '0')), Decimal(observed.get('minimum', '0')))
            merged.update(minimum=format(floor, 'f'), source=current['source'] if current['source'] == observed['source'] else 'multiple')
            balances = [Decimal(c['balance']) for c in (current, observed) if c.get('balance') is not None]
            if balances:  # any finite balance is bounded by the strictest minimum of every alias
                merged['balance'] = format(min(balances), 'f')
                if min(balances) <= floor:
                    merged.update(state='none', usable=False, reason='no usable credit balance above the configured minimum')
    merged['evidence_present'] = bool(current.get('evidence_present') or observed.get('evidence_present'))
    return merged


def account_denied(pool):
    return bool((pool.get('credits') or {}).get('account_denied'))


def capacities(config, usage_root=None, now=None):
    now = now or datetime.now(timezone.utc)
    usage_root = usage_root or Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'omarchy/agents/usage'
    policy = config.get('routing', {})
    ttl = policy.get('telemetry_ttl_seconds', 300)
    minimums = config.get('budget', {}).get('credit_minimum', {})
    providers = {p: read_capacity(p, usage_root, now, ttl, minimums.get(p, 0) if isinstance(minimums, dict) else 0) for p in config.get('providers', [])}
    pools = {}
    for provider, row in providers.items():
        key = pool_id(provider, policy)
        pool = pools.setdefault(key, dict(id=key, providers=[], state='unknown', remaining=None, used=None, incomplete=False, resets_at=None, updated_at=None, reason='shared account pool', credits=None))
        pool['providers'].append(provider)
        pool['credits'] = merge_credits(pool['credits'], row['credits'])
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
