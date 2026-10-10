"""Output contract shared by every command (see SPEC.md, "Output contract")."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SCHEMA = "reaxctl/1"

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REQUIREMENT = 10
EXIT_NEEDS_HUMAN = 11
EXIT_NOT_AVAILABLE = 12
EXIT_NETWORK = 13
EXIT_VERIFY = 20
EXIT_UNEXPECTED = 1

_STATUS_EXIT = {"ok": EXIT_OK, "needs_human": EXIT_NEEDS_HUMAN, "blocked": EXIT_REQUIREMENT, "error": EXIT_UNEXPECTED}


@dataclass
class Result:
    command: str
    status: str = "ok"  # ok | needs_human | blocked | error
    summary: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    needs_human: Optional[Dict[str, Any]] = None
    next: List[Dict[str, str]] = field(default_factory=list)
    exit_code: Optional[int] = None  # overrides the status default (e.g. 12, 13, 20)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def code(self) -> int:
        return self.exit_code if self.exit_code is not None else _STATUS_EXIT.get(self.status, EXIT_UNEXPECTED)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": SCHEMA,
            "command": self.command,
            "ok": self.ok,
            "status": self.status,
            "summary": self.summary,
            "data": self.data,
            "warnings": self.warnings,
            "needs_human": self.needs_human,
            "next": self.next,
        }

    def render_text(self) -> str:
        if self.command == 'doctor':
            lines = ['Verdict', '  [{}] {}'.format(self.status, self.summary),
                     'What works today (rehearsal)',
                     '  ' + ('Localnet rehearsal is possible with the pinned source and tools.' if self.data.get('rehearsal_possible') else ('The practice run needs the REAX source, which the release descriptor does not yet mark as available.' if self.data.get('rehearsal_capable') else 'Install Python venv support (Debian/Ubuntu: python3-venv) before rehearsal.' if any(b.get('id') == 'python_venv' for b in self.data.get('blockers', [])) else 'Use a Python with tarfile extraction filters (3.10.12+, 3.11.4+ or 3.12+) before rehearsal.' if any(b.get('id') == 'python_tarfile' for b in self.data.get('blockers', [])) else 'Install the required Python version before rehearsal.')),
                     'What is needed for real mining']
            for item in self.data.get('blockers', []):
                lines.extend(['  ' + item['message'], '    Fix: ' + item['fix']])
            if not self.data.get('real_mining_possible'):
                lines.append('  Check the release descriptor: the network must be live, with a netuid and public images.')
            blocker_warnings = {item['message'] + ' Fix: ' + item['fix'] for item in self.data.get('blockers', [])}
            lines.extend('  Note: ' + w for w in self.warnings if w not in blocker_warnings)
            lines.append('Next')
            lines.extend('  ' + n.get('command', '') + '  # ' + n.get('why', '') for n in self.next)
            return '\n'.join(lines)
        lines = [f"[{self.status}] {self.summary}".rstrip()]
        if self.command == 'wallet status' and 'rehearsal_wallets' in self.data:
            rehearsal = self.data['rehearsal_wallets']
            lines.append('  Rehearsal wallets: ' + rehearsal['path'])
            for wallet in rehearsal['wallets']:
                lines.append('    ' + wallet['name'] + ': ' + str(wallet.get('hotkey_ss58') or wallet.get('coldkeypub_ss58') or 'public address unavailable'))
        if self.data.get('detail'):
            lines.append(f"  detail: {self.data['detail']}")
        if self.command == 'plan':
            for i, step in enumerate(self.data.get('steps', []), 1):
                extra = ' (blocked: ' + step['blocked'] + ')' if step.get('blocked') else ''
                lines.append(f"  {i}. {step['id']} [{step['who']}]: {step.get('command') or step.get('instructions', '')}{extra}")
        for check in self.data.get('checklist', []):
            if not check.get('pass'):
                lines.append(f"  failed: {check['item']}: {check.get('fix', '')}")
        for name, proc in (self.data.get('processes') or {}).items():
            if isinstance(proc, dict) and 'running' in proc:
                lines.append(f"  {name}: {'running' if proc['running'] else 'stopped'}")
        for w in self.warnings:
            lines.append(f"  warning: {w}")
        if self.needs_human:
            nh = self.needs_human
            lines.append(f"  HUMAN ACTION: {nh.get('action', '')}")
            if nh.get("why"):
                lines.append(f"    why: {nh['why']}")
            for i, step in enumerate(nh.get("steps", []), 1):
                lines.append(f"    {i}. {step}")
            if nh.get("link"):
                lines.append(f"    link: {nh['link']}")
        for n in self.next:
            lines.append(f"  next: {n.get('command', '')}  # {n.get('why', '')}")
        return "\n".join(lines)


def emit(result: Result, as_json: bool) -> int:
    if as_json:
        sys.stdout.write(json.dumps(result.to_dict(), indent=2, sort_keys=False) + "\n")
    else:
        sys.stdout.write(result.render_text() + "\n")
    return result.code()
