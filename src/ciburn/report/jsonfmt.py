from __future__ import annotations

import json

from ciburn.audit import AuditResult


def render_json(result: AuditResult) -> str:
    return json.dumps(result.as_dict(), indent=2, sort_keys=False, default=str) + "\n"
