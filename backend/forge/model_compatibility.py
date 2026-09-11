"""Read-only compatibility checks for human-owned model configurations.

Errors identify configuration IDs, not only upstream names (which can repeat).
Only parameter names and validation reasons are rendered, never their values,
connection bodies, credentials, or frozen runtime snapshots.
"""
from sqlalchemy import select

from .db import Agent, ModelProfile
from .providers import PARAMETERS, RESERVED, validate_parameters


def _parameter_error(connection, declared, parameters):
    protocol = connection['protocol']
    try:
        validate_parameters(protocol, declared, parameters, connection.get('protocol_config'))
    except ValueError as error:
        reason = str(error)
        supported = (set(connection.get('protocol_config', {}).get('supported_parameters', []))
                     if protocol == 'custom_http' else PARAMETERS.get(protocol, set()))
        unknown = set(declared) - supported
        undeclared = (set(parameters) - set(declared)) | (set(parameters) & RESERVED)
        if unknown:
            reason += '；协议不支持的参数名：' + ', '.join(sorted(unknown))
        if undeclared:
            reason += '；未声明或受保护的参数名：' + ', '.join(sorted(undeclared))
        if protocol == 'openai_responses' and (set(declared) | set(parameters)) & {'max_tokens', 'max_completion_tokens'}:
            reason += '；Responses 输出上限使用 max_output_tokens，请人工确认并同时调整允许参数与参数配置'
        return reason
    return None


def model_errors(config, connection, config_id=None, agents=()):
    """Return every relevant model/bound-override issue without changing state."""
    context = (f'模型配置「{config.get("name", "未命名")}」'
               f'（型号 {config.get("model_id", "未填写")}；配置 ID {config_id or "未保存"}；'
               f'协议 {connection["protocol"]}）')
    errors = []
    declared, parameters = config.get('allowed_parameters', []), config.get('parameters', {})
    reason = _parameter_error(connection, declared, parameters)
    if reason:
        errors.append(f'{context}：{reason}')
    if (connection['protocol'] == 'custom_http' and config.get('supports_tools') and
            not connection.get('protocol_config', {}).get('response', {}).get('calls')):
        errors.append(f'{context}：已声明支持工具调用，但自定义协议未配置 calls 和 call_fields；'
                      '请补齐工具调用响应映射，或人工核对模型能力声明')
    for agent in agents:
        cfg = agent.config
        if (config_id not in [cfg.get('model_profile_id'), *cfg.get('fallback_model_ids', [])]
                or not cfg.get('model_parameters')):
            continue
        reason = _parameter_error(connection, declared, parameters | cfg['model_parameters'])
        if reason:
            errors.append(f'{context}：Agent「{cfg.get("name", agent.id)}」（Agent ID {agent.id}）'
                          f'的模型参数覆盖不兼容：{reason}；请编辑该成员的模型参数覆盖')
    return errors


def validate_provider_change(s, provider_id, connection):
    """Call under the provider write lock, before keys/revisions/audit are saved."""
    from .provider_catalog import CatalogModelState
    retired = set(s.scalars(select(CatalogModelState.id).where(
        CatalogModelState.provider_id == provider_id, CatalogModelState.retired.is_(True))))
    agents = list(s.scalars(select(Agent).order_by(Agent.id)))
    errors = []
    models = s.scalars(select(ModelProfile).where(
        ModelProfile.config['provider_id'].as_string() == provider_id).order_by(ModelProfile.id))
    for model in models:
        # Disabled configurations are still editable assets; only retired
        # current-list entries and immutable history are outside this check.
        if model.id not in retired:
            errors.extend(model_errors(model.config, connection, model.id, agents))
    if errors:
        raise ValueError('供应商协议变更未保存：以下当前模型配置或 Agent 参数与目标协议不兼容。\n'
                         + '\n'.join(errors)
                         + '\n请先人工调整兼容参数后重试；若新旧协议不能共用参数，可新建供应商连接，'
                         '在新连接下创建模型配置并明确调整 Agent 绑定。原连接、密钥、模型、绑定与历史均未修改。')
