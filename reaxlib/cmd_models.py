"""`reaxctl models`: the models you can mine, their status, their hardware need, and whether this machine fits."""
from __future__ import annotations

from . import descriptor, models, state, sysprobe
from .cli import add_common
from .result import Result

ORDER = {'live': 0, 'coming': 1, 'retired': 2}


def host_view(args, release, probe=None, host=None):
    """Read-only machine facts for the fit check: one `nvidia-smi` call, no Docker probes."""
    probe = probe or sysprobe.run
    host = host or sysprobe.facts(state.home(args), state.load_config(args).get('miner_port', 8091))
    return host, models.detect_gpus(lambda cmd: probe(cmd, timeout=10), host)


def describe(release, entry, host, gpus, installed):
    verdict = models.fit(release, entry, host, gpus)
    ok, why = models.availability(release, entry)
    runtime, throughput = entry['runtime'], entry.get('unit_throughput')
    row = {'id': entry['id'], 'name': entry['name'], 'pot': entry['pot'], 'status': entry['status'], 'kind': entry['kind'],
            'installable': ok, 'not_installable_because': why or None,
            'accelerator': entry['accelerator'], 'needs': models.need_text(entry), 'gpu': {k: entry['gpu'][k] for k in ('class', 'min_vram_gb', 'max_vram_gb', 'count', 'vendor') if k in entry['gpu']},
            'disk_gb': entry['disk_gb'], 'weights': {k: entry['weights'][k] for k in ('repo', 'revision', 'size_gb', 'gated', 'licence') if k in entry['weights']},
            'runtime': {'image': runtime['image'], 'digest': runtime['digest'], 'port': runtime['port']},
            'unit_throughput': throughput, 'manifest_id': entry['manifest_id'], 'commitment': entry['commitment'],
            'fits': verdict['fits'], 'fit_reasons': verdict['reasons'], 'installed': installed == entry['id']}
    if entry.get('notes'):
        row['notes'] = entry['notes']
    return row


def table(rows):
    cells = [('MODEL', 'STATUS', 'THIS MACHINE', 'NEEDS', 'NAME')]
    for r in rows:
        fit = ('fits' if r['fits'] else 'does not fit') if r['status'] != 'retired' else '-'
        cells.append((r['id'], r['status'], fit, r['needs'], r['name'] + ('  (installed)' if r['installed'] else '')))
    widths = [max(len(row[i]) for row in cells) for i in range(4)]
    return ['  '.join(row[i].ljust(widths[i]) for i in range(4)) + '  ' + row[4] for row in cells]


def handle(args):
    release = descriptor.load(args, fetch=True)
    config = state.load_config(args)
    host, gpus = host_view(args, release)
    entries = models.catalog(release)
    if args.status:
        entries = [e for e in entries if e['status'] == args.status]
    elif not args.all:
        entries = [e for e in entries if e['status'] != 'retired' or e['id'] == config.get('pool')]
    rows = sorted((describe(release, e, host, gpus, config.get('pool')) for e in entries), key=lambda r: (ORDER[r['status']], r['id']))
    live_fit = [r for r in rows if r['status'] == 'live' and r['fits'] and r['installable']]
    data = {'models': rows, 'installed_model': config.get('pool'), 'release_id': release['release_id'],
            'machine': {'gpus': [{'name': g['name'], 'vram_gb': round(g['vram_gb'], 1) if g.get('vram_gb') is not None else None, 'driver_ok': g.get('driver_ok')} for g in gpus],
                        'cpu_cores': host.get('cpu_cores'), 'ram_gb': round(host['ram_gb'], 1) if host.get('ram_gb') is not None else None,
                        'disk_free_gb': round(host['disk_free_gb'], 1) if host.get('disk_free_gb') is not None else None},
            'text_lines': table(rows)}
    for row in rows:
        if row['status'] == 'live' and row.get('notes'):
            data['text_lines'].append('{}: {}'.format(row['id'], row['notes']))
    # a suggestion only: the most demanding live model this machine can serve
    best = max(live_fit, key=lambda r: (r['gpu'].get('min_vram_gb', 0), r['id'])) if live_fit else None
    nxt = [{'command': 'reaxctl up --model ' + best['id'], 'why': 'Set up this machine for the most demanding live model that fits it.'}] if best else []
    if not live_fit and any(r['status'] == 'live' for r in rows):
        data['text_lines'].append('No live model fits this machine yet; see the fit_reasons in --json.')
    return Result('models', summary='{} model(s); {} live and fit this machine.'.format(len(rows), len(live_fit)), data=data, next=nxt)


def register(sub):
    p = sub.add_parser('models', help='list models you can mine, with status, hardware need and fit')
    add_common(p)
    p.add_argument('--status', choices=models.STATUSES, default=None, help='only models with this status')
    p.add_argument('--all', action='store_true', help='include retired models')
    p.set_defaults(func=handle)
