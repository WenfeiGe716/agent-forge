"""Translate Forge execution profiles into Codex runtime policy values.

This is deliberately a small deep interface: callers provide the frozen Forge
permission profile and receive the complete Codex policy tuple.  No caller may
silently omit a field or turn an unknown profile into a weaker policy.
"""

from typing import Literal

PermissionProfile = Literal['ask', 'auto', 'full']

_POLICIES = {
    'ask': {
        'approval_policy': 'on-request',
        'approvals_reviewer': 'user',
        'sandbox': 'workspace-write',
    },
    'auto': {
        'approval_policy': 'on-request',
        'approvals_reviewer': 'auto_review',
        'sandbox': 'workspace-write',
    },
    'full': {
        'approval_policy': 'never',
        'approvals_reviewer': 'user',
        'sandbox': 'danger-full-access',
    },
}


def codex_execution_policy(profile: str) -> dict[str, str]:
    """Return a fresh, complete Codex policy for one frozen Forge profile."""
    if profile not in _POLICIES:
        raise ValueError(f'未知执行权限档：{profile}')
    return dict(_POLICIES[profile])
