import {Agent,Json} from './types';

/** Merge an immutable run snapshot without assuming every historical snapshot
 * contains a complete model binding. Older runs may omit models or keep an
 * empty list; those records must remain inspectable instead of crashing React.
 */
export function mergeAgentSnapshot(agent:Agent,frozen:Json|undefined):Agent{
 if(!frozen||typeof frozen!=='object'||Array.isArray(frozen))return agent;
 const models=Array.isArray(frozen.models)?frozen.models:[];
 const profile=models.find(model=>model&&typeof model==='object'&&typeof model.profile_id==='string')?.profile_id;
 return {...agent,
  name:typeof frozen.name==='string'?frozen.name:agent.name,
  role:typeof frozen.role==='string'?frozen.role:agent.role,
  job_title:typeof frozen.job_title==='string'?frozen.job_title:agent.job_title,
  department:typeof frozen.department==='string'?frozen.department:agent.department,
  communication:frozen.communication??agent.communication,
  office_position:frozen.office_position??agent.office_position,
  design_ref:frozen.design_ref??agent.design_ref,
  role_template_ref:frozen.role_template_ref??agent.role_template_ref,
  tool_permissions:Array.isArray(frozen.tools)?frozen.tools:agent.tool_permissions,
  model_profile_id:profile??agent.model_profile_id,
 };
}
