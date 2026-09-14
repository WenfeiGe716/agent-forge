/** Original furniture; dimensions match existing navigation and seating contracts. */
import {V3,modelMatrix,multiply} from './scene';
import {Part,color,bevel,segment,oriented} from './officeRenderer';
const mix=(a:number,b:number,t:number):number=>{const x=color(a),y=color(b),c=x.map((v,i)=>Math.round((v*(1-t)+y[i]*t)*255));return(c[0]<<16)|(c[1]<<8)|c[2];};
type FurnitureFinish='classic'|'department';
export function chairKit(x:number,z:number,accent:number,yaw=0,seatTop=.715,finish:FurnitureFinish='classic'):Part[]{
 const parts:Part[]=[],delta=seatTop-.715,parent=modelMatrix([x,delta,z],[1,1,1],yaw);
 const put=(p:Part)=>{p.matrix=multiply(parent,p.matrix||modelMatrix(p.p,p.s,p.yaw));parts.push(p);};
 const b=(p:V3,s:V3,h:number,r=.035,surf:Part['surface']='matte',tag='chair')=>put(bevel(p,s,h,r,{surface:surf,tag}));
 const c=(p:V3,s:V3,h:number,mesh='cylinder',tag='chair')=>put({p,s,color:color(h),mesh,surface:'metal',tag});
 // Role colour is intentionally only a restrained undertone.  Saturated
 // upholstery made the office read like a toy UI and competed with the
 // character labels; real furniture uses a charcoal/stone base with a subtle
 // team tint in the piping and small details.
 const upholstery=mix(accent,0x737b7a,.74);
 b([0,.579,0],[.715,.11,.68],0x313e4b,.05,'matte','chair-seat-shell');
 b([0,.650,0],[.667,.13,.63],upholstery,.059,'cloth','chair-seat-cushion');
 b([0,.709,.017],[.60,.012,.53],mix(upholstery,0xd2d9da,.2),.006,'cloth','chair-seat-piping');
 // The rear remains a readable dark shell; role colour is limited to the
 // subtle textile undertone. Finish changes never move the seat or contacts.
 b([0,.996,.32],[.708,.682,.096],finish==='department'?mix(accent,0x596260,.78):0x35424e,.039,'matte','chair-back-frame');
 b([0,1.004,.252],[.609,.577,.086],upholstery,.04,'cloth','chair-back-padding');
 b([0,.849,.187],[.51,.12,.08],mix(upholstery,0x2f4350,.18),.035,'cloth','chair-lumbar');
 for(const side of[-1,1]){
  b([side*.252,1.012,.206],[.011,.465,.005],mix(upholstery,0xc3ced2,.24),.002,'cloth','chair-seam');
  b([side*.407,.93,.017],[.13,.055,.36],0x3c4954,.025,'rubber','chair-armrest');
  c([side*.392,.755,.14],[.045,.335,.045],0x6e808d,'cylinder','chair-arm-support');
 }
 c([0,.395,0],[.079,.33,.079],0xb1bec5,'cylinder','chair-piston');
 c([0,.24,0],[.20,.13,.20],0x4b5d6b,'cylinder','chair-hub');
 for(let i=0;i<5;i++){
  const a=i/5*Math.PI*2,px=Math.cos(a)*.365,pz=Math.sin(a)*.365,q=segment([0,.260,0],[px,.226,pz],.068,color(0x7f929d));q.surface='metal';q.tag='chair-base-spoke';put(q);
  put({p:[px,.178,pz],s:[.12,.06,.12],matrix:oriented([px,.178,pz],[.12,.067,.12],a,Math.PI/2),color:color(0x303b49),mesh:'cylinder',surface:'rubber',tag:'chair-caster'});
 }
 for(const q of parts)q.assetOrigin=[x,0,z];return parts;
}
export function deskKit(x:number,z:number,accent:number,index=0,finish:FurnitureFinish='classic'):Part[]{
 const out:Part[]=[],p=(a:V3):V3=>[x+a[0],a[1],z+a[2]];
 // Thin markings and sub-centimetre details do not benefit from dense bevel topology.
 const b=(a:V3,s:V3,h:number,r=.025,surface:Part['surface']='matte',tag='desk')=>out.push(Math.min(...s)<.019?{p:p(a),s,color:color(h),mesh:'cube',surface,tag}:bevel(p(a),s,h,r,{surface,tag}));
 const part=(a:V3,s:V3,h:number,mesh='cylinder',surface:Part['surface']='matte',tag='desk')=>{const q:Part={p:p(a),s,color:color(h),mesh,surface,tag};out.push(q);return q;};
 const department=finish==='department';
 // Keep the historical desk-oak-top identity: consumers count real desks by
 // this tag. The department finish only changes material, not tabletop height.
 b([0,1.011,0],[2.60,.136,1.02],department?0xf0f0e9:0xe4d5bc,.042,department?'matte':'wood','desk-oak-top');
 b([0,.944,0],[2.535,.021,.96],department?0xd9dfdc:0xc2ad8e,.009,department?'matte':'wood','desk-edge');
 for(const side of[-1,1]){b([side*1.08,.502,0],[.072,.884,.070],0xe3e7e5,.025,'metal','desk-metal-leg');b([side*1.08,.15,0],[.122,.074,.73],0x445562,.024,'rubber','desk-foot');}
 for(const side of[-1,1])b([side*1.08,.911,0],[.08,.054,.77],0xadb8bf,.023,'metal','desk-top-support');
 b([0,.775,-.3],[2.19,.13,.07],0x8d9da7,.027,'metal','desk-crossbar');
 b([0,.893,-.40],[1.8,.065,.15],0x87979c,.019,'metal','cable-tray');
 b([0,1.092,.219],[1.27,.014,.49],department?0xc5ceca:0x707f7b,.007,'rubber','desk-mat');
 b([0,1.505,-.20],[1.17,.732,.084],0x323b40,.034,'metal','monitor-frame');
 b([0,1.520,-.250],[.89,.48,.018],department?mix(accent,0xffffff,.10):0x354651,.008,department?'matte':'metal','monitor-back-panel');
 for(let i=0;i<7;i++)b([-.26+i*.085,1.294,-.266],[.047,.006,.004],0x192c39,.001,'matte','monitor-vent');
 b([0,1.56,-.267],[.066,.073,.004],0x647d87,.001,'metal','monitor-mark');
 b([0,1.512,-.151],[1.085,.646,.013],0x172732,.006,'screen','monitor-screen');
 b([0,1.132,-.216],[.080,.242,.076],0x8d9da7,.02,'metal','monitor-column');
 b([0,1.09,-.18],[.40,.03,.29],0xabb8be,.012,'metal','monitor-base');
 part([.51,1.181,-.149],[.018,.018,.006],0x91d3b5,'sphere','screen','monitor-led').emissive=.75;
 // Abstract editor motif; not invented task output.
 b([0,1.797,-.141],[1.057,.05,.006],0x263d4b,.002,'screen','screen-titlebar');
 for(let j=0;j<3;j++)part([-.477+j*.034,1.801,-.136],[.014,.014,.004],[0xdf9a8d,0xddc48b,0x95bca7][j],'eye','screen','screen-dot').emissive=.3;
 b([-.389,1.49,-.139],[.237,.481,.004],0x233641,.001,'screen','editor-sidebar');
 for(let row=0;row<6;row++){const y=1.717-row*.076;b([-.407,y,-.135],[.144-(row%2)*.045,.013,.003],0x597482,.001,'screen','editor-sidebar-line');const w=.24+(row%3)*.066;b([-.17+(row%2)*.064,y,-.132],[.06,.012,.005],accent,.001,'screen','editor-syntax');b([.05+(row%2)*.062,y,-.132],[w,.014,.005],row%2?0x829ca9:0x739e96,.001,'screen','editor-line');}
 b([-.077,1.115,.231],[.859,.027,.304],0xadb8bd,.013,'metal','keyboard-base');
 for(let row=0;row<4;row++)for(let col=0;col<12;col++)out.push({p:p([-.457+col*.070,1.137,.117+row*.066]),s:[.058,.016,.044],mesh:'cube',color:color((row===0&&col===0)?accent:0xe4e6df),surface:'matte',tag:'keycap'});
 b([-.067,1.148,.307],[.34,.010,.044],0xe9ebe5,.004,'matte','spacebar');
 part([.530,1.14,.222],[.164,.08,.228],0xc7d0d1,'sphere','matte','mouse');b([.530,1.18,.203],[.016,.011,.045],0x577180,.004,'rubber','mouse-wheel');
 part([.974,1.199,-.195],[.199,.239,.199],0xe8e5da,'cylinder','matte','desk-mug');part([.974,1.321,-.195],[.17,.008,.17],0x80644d,'cylinder','matte','coffee-surface');
 const rim=part([.974,1.322,-.195],[.204,.204,.014],0xf5f1e8,'ring','matte','cup-lip');rim.matrix=oriented(rim.p,rim.s,0,Math.PI/2);part([1.107,1.207,-.195],[.134,.16,.11],0xe8e5da,'ring','matte','cup-handle');
 b([-.984,1.112,.21],[.35,.042,.46],mix(accent,0x354658,.40),.015,'cloth','notebook-cover');b([-.969,1.126,.21],[.305,.018,.421],0xeae5d9,.007,'matte','notebook-pages');b([-.984,1.140,.21],[.35,.009,.46],mix(accent,0x354658,.40),.004,'cloth','notebook-cover');
 const pen=segment(p([-.89,1.158,.06]),p([-.86,1.158,.356]),.014,color(0x33444f));pen.surface='metal';pen.tag='pen';out.push(pen);
 part([-.979,1.092,-.295],[.247,.03,.247],0xb8c2c6,'cylinder','metal','lamp-base');
 for(const [a,e]of[[[-.979,1.11,-.295],[-1.039,1.519,-.295]],[[-1.039,1.519,-.295],[-.76,1.707,-.26]]] as [V3,V3][]){const q=segment(p(a),p(e),.031,color(0xa5b2b8));q.surface='metal';q.tag='lamp-arm';out.push(q);}
 b([-.721,1.727,-.224],[.34,.061,.16],0xe4e6df,.025,'matte','lamp-shade');b([-.721,1.691,-.224],[.28,.007,.112],0xf4e7c2,.003,'screen','lamp-diffuser');out[out.length-1].emissive=.6;
 if(department){
  // A low divider and planted rail form a repeated white workstation island.
  // Everything is inside the existing 2.7 x 1.08 desk obstacle, behind the
  // unchanged monitor/keyboard contacts; there is no new aisle collision.
  b([0,1.242,-.464],[2.42,.31,.096],0xebece5,.033,'matte','island-low-divider');
  b([0,1.237,-.410],[2.28,.065,.009],accent,.004,'matte','island-divider-accent');
  b([0,1.445,-.45],[2.42,.13,.16],0xe4e9df,.032,'matte','island-planter');
  for(let i=0;i<5;i++)part([-.92+i*.46,1.56+(i%2)*.012,-.447],[.56,.16+(i%2)*.05,.17],i%2?0x73916b:0x648660,'sphere','matte','island-planting');
 }
 for(const q of out)q.assetOrigin=[x,0,z];return out;
}
export function plantKit(x:number,z:number,scale=1):Part[]{
 const out:Part[]=[],p=(a:V3):V3=>[x+a[0]*scale,a[1]*scale,z+a[2]*scale];
 out.push({p:p([0,.31,0]),s:[.67*scale,.54*scale,.67*scale],color:color(0xd0cdc1),mesh:'pot',surface:'matte',tag:'ceramic-planter'});
 out.push({p:p([0,.577,0]),s:[.49*scale,.013,.49*scale],color:color(0x554941),mesh:'cylinder',surface:'matte',tag:'plant-soil'});
 for(let i=0;i<9;i++){
  const a=i*2.39996,r=.26+(i%3)*.095,y=1.01+(i%4)*.19,start=p([0,.56,0]),tip=p([Math.cos(a)*r,y,Math.sin(a)*r]);const stem=segment(start,tip,.018*scale,color(0x657853));stem.surface='matte';stem.tag='plant-stem';out.push(stem);
  const end=p([Math.cos(a)*(r+.25),y+.27,Math.sin(a)*(r+.25)]),leaf=segment(tip,end,.73*scale,color(i%2?0x537e62:0x729878));leaf.mesh='leaf';leaf.surface='matte';leaf.roughness=.55;leaf.tag='plant-leaf';out.push(leaf);
 }for(const q of out)q.assetOrigin=[x,0,z];return out;
}
