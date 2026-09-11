import type {DesignAsset} from '../components/DesignCenter';
import type {OrganizationLayout} from '../types';
import {addFloor,upgradeLayout} from './organizationDesign';

/** Open a local editable copy, preserving pending drafts and the applied revision. No writes. */
export function prepareOrganizationDraft(current:OrganizationLayout,latest:DesignAsset|null,appendFloor:boolean){
 if(current.ref&&(!latest||latest.id!==current.ref.id||latest.kind!=='office'))throw Error('当前布局引用已改变，请刷新后重试。');
 const pending=latest?.status==='draft';
 const seed:DesignAsset=pending?latest!:{id:latest?.id||'',kind:'office',name:latest?.name||'组织楼层与部门布局',version:current.ref?.version||0,status:'draft',digest:'',config:current.config,source:'manual'};
 const asset:DesignAsset=JSON.parse(JSON.stringify(seed));
 let config=asset.config;
 if(appendFloor){
  if(!Array.isArray(config.desks))config=config.floors?.length?upgradeLayout(config):Object.keys(config.positions||{}).length?(()=>{throw Error('旧布局尚无部门楼层。请先在布局编辑器中补齐楼层，再新增部门。');})():{...config,layout_version:2,floors:[],positions:{},teams:[],desks:[]};
  config=addFloor(config);
 }
 asset.config=config;
 return{asset,expectedVersion:latest?.version||0,history:latest?.versions|| (latest?[latest]:[]),focusFloor:appendFloor?config.floors.at(-1)?.floor:undefined,pending};
}
