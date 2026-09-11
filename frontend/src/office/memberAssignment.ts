import {Agent,OrganizationLayout} from '../types';

/** Business assignment is current Agent configuration; physical placement is an applied design version. */
export function memberAssignment(agent:Agent,organization?:OrganizationLayout|null){
 const config=organization?.config,physical=Array.isArray(config?.desks);
 const desk=physical?config!.desks!.find(d=>d.agent_id===agent.id):agent.office_position;
 const floor=config?.floors?.find(f=>f.floor===desk?.floor);
 const team=config?.teams?.find(t=>t.member_ids.includes(agent.id));
 const department=agent.department?.trim()||'';
 const spatialMismatch=!!floor&&department!==floor.department;
 const workplace=desk?`${desk.floor} 楼`:config?.floors?.length?'工位待安排':'工位未配置';
 return{department,desk,floor,team,spatialMismatch,workplace,
  label:`${department||'未分配部门'} · ${workplace}${team?' · '+team.name:''}${spatialMismatch?' · 空间待调整':''}`};
}
