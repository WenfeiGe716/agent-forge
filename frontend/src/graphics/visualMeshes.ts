/** Original offline geometry. Positions and smooth normals, no network assets.
 * Physical bevel radii are expressed in scene units, not stretched with a cube.
 */
import {V3} from './scene';
export const unit=(v:V3):V3=>{const d=Math.hypot(...v)||1;return v.map(n=>n/d) as V3;};
const vertex=(out:number[],p:V3,n:V3)=>out.push(...p,...n);
export function surface(rows:number,cols:number,point:(u:number,v:number)=>[V3,V3]):Float32Array{
 const out:number[]=[];
 for(let j=0;j<rows;j++)for(let i=0;i<cols;i++){
  const a=point(i/cols,j/rows),b=point((i+1)/cols,j/rows),c=point((i+1)/cols,(j+1)/rows),d=point(i/cols,(j+1)/rows);
  for(const [p,n] of [a,b,c,a,c,d])vertex(out,p,n);
 }
 return new Float32Array(out);
}
export function smoothSphere(rows=12,cols=20):Float32Array{
 return surface(rows,cols,(u,v)=>{const a=u*Math.PI*2,b=v*Math.PI,n:V3=[Math.cos(a)*Math.sin(b),Math.cos(b),Math.sin(a)*Math.sin(b)];return[n.map(x=>x*.5) as V3,n];});
}
export function capsuleMesh(rows=12,cols=16):Float32Array{
 return surface(rows,cols,(u,v)=>{const a=u*Math.PI*2,b=v*Math.PI,n:V3=[Math.cos(a)*Math.sin(b),Math.cos(b),Math.sin(a)*Math.sin(b)];const y=Math.cos(b)*.24+(v<.5?.26:-.26);return[[n[0]*.5,y,n[2]*.5],unit([n[0],n[1]*2.08,n[2]])];});
}
export function cylinderMesh(cols=24):Float32Array{
 const out:number[]=[];
 for(let i=0;i<cols;i++){
  const a=i/cols*Math.PI*2,b=(i+1)/cols*Math.PI*2,n:V3=[Math.cos(a),0,Math.sin(a)],m:V3=[Math.cos(b),0,Math.sin(b)];
  const p=(r:V3,y:number):V3=>[r[0]*.5,y,r[2]*.5];
  for(const [q,r] of [[p(n,-.5),n],[p(n,.5),n],[p(m,.5),m],[p(n,-.5),n],[p(m,.5),m],[p(m,-.5),m]] as [V3,V3][])vertex(out,q,r);
  for(const y of [-.5,.5]){const normal:V3=[0,Math.sign(y),0];vertex(out,[0,y,0],normal);vertex(out,p(y>0?m:n,y),normal);vertex(out,p(y>0?n:m,y),normal);}
 }
 return new Float32Array(out);
}
export function bevelBox(w=1,h=1,d=1,r=.08,detail=2):Float32Array{
 if(![w,h,d,r].every(Number.isFinite)||Math.min(w,h,d)<=0)throw Error('Invalid bevel dimensions');
 r=Math.max(.0001,Math.min(r,Math.min(w,h,d)/2-.00001));const half=[w/2,h/2,d/2],out:number[]=[];
 const axes=(axis:number)=>{const a=half[axis],values:number[]=[];for(let k=0;k<=detail;k++)values.push(-a+r-r*Math.cos(k/detail*Math.PI/2));for(let k=0;k<=detail;k++)values.push(a-r+r*Math.sin(k/detail*Math.PI/2));return values;};
 for(const [n,u,v] of [[[1,0,0],[0,1,0],[0,0,1]],[[-1,0,0],[0,0,1],[0,1,0]],[[0,1,0],[0,0,1],[1,0,0]],[[0,-1,0],[1,0,0],[0,0,1]],[[0,0,1],[1,0,0],[0,1,0]],[[0,0,-1],[0,1,0],[1,0,0]]] as [V3,V3,V3][]){
  const ni=n.findIndex(x=>x!==0),ui=u.findIndex(x=>x!==0),vi=v.findIndex(x=>x!==0),us=axes(ui),vs=axes(vi);
  const point=(a:number,b:number):[V3,V3]=>{const p=n.map((x,i)=>x*half[ni]+u[i]*a+v[i]*b) as V3,c=p.map((x,i)=>Math.max(-half[i]+r,Math.min(half[i]-r,x))) as V3,normal=unit(p.map((x,i)=>x-c[i]) as V3);return[c.map((x,i)=>x+normal[i]*r) as V3,normal];};
  for(let i=0;i<us.length-1;i++)for(let j=0;j<vs.length-1;j++){const a=point(us[i],vs[j]),b=point(us[i+1],vs[j]),c=point(us[i+1],vs[j+1]),d=point(us[i],vs[j+1]);for(const [p,n] of [a,b,c,a,c,d])vertex(out,p,n);}
 }
 return new Float32Array(out);
}
/** Elliptical ring loft with tapered cheeks/shoulders. Values: y, radiusX, radiusZ, zOffset. */
export function loft(rings:[number,number,number,number][],cols=24):Float32Array{
 const out:number[]=[];
 const point=(ring:number,i:number):[V3,V3]=>{const [y,x,z,off]=rings[ring],a=i/cols*Math.PI*2,prev=rings[Math.max(0,ring-1)],next=rings[Math.min(rings.length-1,ring+1)],dy=next[0]-prev[0]||1,dx=(next[1]-prev[1])/dy,dz=(next[2]-prev[2])/dy;return[[x*Math.cos(a),y,z*Math.sin(a)+off],unit([Math.cos(a)/Math.max(x,.001),-(dx*Math.cos(a)**2/Math.max(x,.001)+dz*Math.sin(a)**2/Math.max(z,.001)),Math.sin(a)/Math.max(z,.001)])];};
 for(let j=0;j<rings.length-1;j++)for(let i=0;i<cols;i++){const a=point(j,i),b=point(j,i+1),c=point(j+1,i+1),d=point(j+1,i);for(const [p,n]of[a,d,c,a,c,b])vertex(out,p,n);}
 return new Float32Array(out);
}
export function hairCap(long=false):Float32Array{
 return surface(12,28,(u,v)=>{const a=u*Math.PI*2,front=(Math.sin(a)+1)/2,end=(long?2.13:1.97)-front*(long?.98:.80),b=v*end;const n:V3=[Math.cos(a)*Math.sin(b),Math.cos(b),Math.sin(a)*Math.sin(b)];return[[n[0]*.335,n[1]*.386,n[2]*.340-.022],n];});
}
export function torusMesh(radial=28,tube=6,ratio=.075):Float32Array{
 // Ring in XY plane, faces +Z. Radius bounds [-.5,.5].
 return surface(tube,radial,(u,v)=>{const a=u*Math.PI*2,b=v*Math.PI*2,R=.5-ratio,n:V3=[Math.cos(a)*Math.cos(b),Math.sin(a)*Math.cos(b),Math.sin(b)];return[[(R+ratio*Math.cos(b))*Math.cos(a),(R+ratio*Math.cos(b))*Math.sin(a),ratio*Math.sin(b)],n];});
}
export function modelMeshes():Map<string,Float32Array>{
 return new Map([
 ['sphere',smoothSphere()],['limb',smoothSphere(6,12)],['capsule',capsuleMesh()],['cylinder',cylinderMesh()],['round',bevelBox(1,1,1,.1,1)],
 ['ring',torusMesh()],['eye',smoothSphere(8,16)],['hair-short',hairCap()],['hair-long',hairCap(true)],
 ['face',loft([[-.31,.025,.03,.025],[-.28,.13,.14,.036],[-.21,.225,.23,.018],[-.08,.277,.267,0],[.08,.287,.272,0],[.23,.244,.22,-.015],[.31,.13,.12,-.025],[.34,.003,.003,-.025]],28)],
 ['torso',loft([[-.31,.16,.125,0],[-.27,.235,.166,0],[-.1,.242,.176,0],[.1,.27,.178,-.014],[.23,.29,.155,-.025],[.30,.16,.11,-.014],[.32,.105,.095,-.01]],24)],
 ['pot',loft([[-.5,.31,.31,0],[-.45,.34,.34,0],[.42,.48,.48,0],[.49,.49,.49,0],[.5,.43,.43,0]],24)],
 ['leaf',loft([[-.5,.001,.003,0],[-.25,.12,.035,.02],[.04,.18,.045,.08],[.30,.12,.032,.10],[.5,.001,.003,.075]],14)]
 ]);
}
