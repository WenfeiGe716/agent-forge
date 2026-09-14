import pytest

from forge.codex_permissions import codex_execution_policy


def test_ask_requires_user_approval_and_workspace_sandbox():
    assert codex_execution_policy('ask') == {
        'approval_policy': 'on-request',
        'approvals_reviewer': 'user',
        'sandbox': 'workspace-write',
    }


def test_auto_routes_approval_to_reviewer_without_removing_request_boundary():
    assert codex_execution_policy('auto') == {
        'approval_policy': 'on-request',
        'approvals_reviewer': 'auto_review',
        'sandbox': 'workspace-write',
    }


def test_full_is_explicit_danger_full_access_and_never_default():
    assert codex_execution_policy('full') == {
        'approval_policy': 'never',
        'approvals_reviewer': 'user',
        'sandbox': 'danger-full-access',
    }


def test_invalid_profile_fails_closed():
    with pytest.raises(ValueError, match='权限档'):
        codex_execution_policy('unknown')
