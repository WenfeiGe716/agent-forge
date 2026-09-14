/** Offline studio renderer. Linear-light material response, soft shadow filtering,
 * GPU-instanced dynamic meshes when ANGLE_instanced_arrays is present, with a
 * real non-instanced fallback. No image, CDN, model service or generated UI state.
 */
import {V3,M4,identity,multiply,modelMatrix,lookAt,ortho,transform,cubeVertices} from './scene';
import {modelMeshes,bevelBox} from './visualMeshes';
import {CameraInsets,officeOverview,officeProjection} from './officeCamera';
export type Surface='matte'|'skin'|'cloth'|'metal'|'wood'|'screen'|'rubber'|'shadow'|'glass'|'stone';
export type Part={p:V3;s:V3;color:V3;mesh?:string;yaw?:number;matrix?:M4;alpha?:number;emissive?:number;roughness?:number;metalness?:number;surface?:Surface;tag?:string;assetOrigin?:V3;layer?:'environment'|'office';nightEmissive?:number;castShadow?:boolean};
export type RenderQuality='detail'|'balanced'|'economy';
export const color=(h:number):V3=>[(h>>16&255)/255,(h>>8&255)/255,(h&255)/255];
export function segment(a:V3,b:V3,width:number,c:V3):Part{
 const d=b.map((x,i)=>x-a[i]),length=Math.hypot(...d)||.001,y=d.map(x=>x/length);const ref=Math.abs(y[0])>.9?[0,0,1]:[1,0,0];let x=[y[1]*ref[2]-y[2]*ref[1],y[2]*ref[0]-y[0]*ref[2],y[0]*ref[1]-y[1]*ref[0]];const l=Math.hypot(...x)||1;x=x.map(v=>v/l);const z=[x[1]*y[2]-x[2]*y[1],x[2]*y[0]-x[0]*y[2],x[0]*y[1]-x[1]*y[0]];
 return{p:[0,0,0],s:[1,1,1],color:c,mesh:'capsule',matrix:new Float32Array([...x.map(v=>v*width),0,...y.map(v=>v*(length+width*.20)),0,...z.map(v=>v*width),0,...a.map((v,i)=>(v+b[i])/2),1])};
}
export function bevel(p:V3,s:V3,h:number,r=.03,extra:Partial<Part>={}):Part{
 return{p,s:[1,1,1],color:color(h),mesh:`bevel:${s.map(v=>v.toFixed(4)).join(':')}:${r.toFixed(4)}`,...extra};
}
export function oriented(p:V3,s:V3,yaw=0,pitch=0,roll=0):M4{
 const cx=Math.cos(pitch),sx=Math.sin(pitch),cz=Math.cos(roll),sz=Math.sin(roll);
 const rx=new Float32Array([1,0,0,0,0,cx,sx,0,0,-sx,cx,0,0,0,0,1]);
 const rz=new Float32Array([cz,sz,0,0,-sz,cz,0,0,0,0,1,0,0,0,0,1]);
 return multiply(modelMatrix(p,[1,1,1],yaw),multiply(rz,multiply(rx,modelMatrix([0,0,0],s))));
}
const SURF:Record<Surface,number>={matte:0,skin:1,cloth:2,metal:3,wood:4,screen:5,rubber:6,shadow:7,glass:8,stone:9};
const defaults:Record<Surface,[number,number]>={matte:[.66,0],skin:[.58,0],cloth:[.94,0],metal:[.28,.82],wood:[.46,0],screen:[.27,.08],rubber:[.92,0],shadow:[1,0],glass:[.17,.05],stone:[.81,0]};
const varying=`varying vec3 N;varying vec3 C;varying vec3 P;varying vec4 L;varying vec4 K;varying float A;varying vec2 U;`;
const fs=`precision highp float;${varying}
uniform sampler2D shadow;uniform float day;uniform vec3 eye;uniform float shadowSize;uniform float softShadows;uniform float environmentQuality;
uniform vec3 focusPoint;uniform float focusPass;
// Only static foreground surfaces fade, in a small sightline around the live
// selected person. The floor, neighbors, background, and shadow map stay whole.
float foreground(){
 vec3 ray=normalize(eye-focusPoint),delta=P-focusPoint;float ahead=dot(delta,ray);
 return smoothstep(.35,.85,ahead)*(1.-smoothstep(.78,1.32,length(delta-ray*ahead)))*smoothstep(.18,.65,P.y);
}
float depthAt(vec2 uv){return dot(texture2D(shadow,uv),vec4(1./16777216.,1./65536.,1./256.,1.));}
vec3 linear(vec3 c){return pow(max(c,vec3(0.)),vec3(2.2));}
vec3 display(vec3 c){return pow(clamp(c,0.,1.),vec3(1./2.2));}
float noise3(vec3 p){return fract(sin(dot(p,vec3(12.9898,78.233,37.719)))*43758.5453);}
float visibility(vec3 n,vec3 lightDir){
 vec3 q=L.xyz/L.w*.5+.5;if(q.x<=0.||q.x>=1.||q.y<=0.||q.y>=1.||q.z<=0.||q.z>=1.)return 1.;
 float bias=max(.00105,.0020*(1.-max(dot(n,lightDir),0.))),lit=0.;
 if(softShadows<.5)return step(q.z-bias,depthAt(q.xy));
 if(softShadows<1.5){for(int x=0;x<2;x++)for(int y=0;y<2;y++){vec2 off=vec2(float(x)-.5,float(y)-.5)*1.6/shadowSize;lit+=step(q.z-bias,depthAt(q.xy+off));}return lit*.25;}
 for(int x=0;x<3;x++)for(int y=0;y<3;y++){vec2 off=vec2(float(x)-1.,float(y)-1.)*1.4/shadowSize;lit+=step(q.z-bias,depthAt(q.xy+off));}return lit/9.;
}
void main(){
 float fade=focusPass<-.5?0.:foreground();
 if(focusPass>-.5&&focusPass<.5&&fade>.001)discard;
 if(focusPass>.5&&fade<=.001)discard;
 float alpha=A*(1.-fade*.88);
 float surface=mod(K.w,16.);
 if(surface>6.5&&surface<7.5){float opacity=(1.-smoothstep(.05,.5,length(U)))*A;gl_FragColor=vec4(C,opacity);return;}
 vec3 n=normalize(N),v=normalize(eye-P),l=normalize(vec3(-.45,.85,.30)),h=normalize(v+l);
 float rough=clamp(K.y,.12,1.),metal=clamp(K.z,0.,1.),nl=max(dot(n,l),0.),nv=max(dot(n,v),.001),nh=max(dot(n,h),0.),vh=max(dot(v,h),0.);
 vec3 base=linear(C);
 if(K.w>15.5&&environmentQuality<1.5){
  // Low-overhead exterior shading retains the same neutral daylight palette.
  float hemi=n.y*.5+.5;vec3 amb=mix(vec3(.23,.215,.20),vec3(.50,.55,.60),hemi);amb=mix(amb*vec3(.21,.26,.43),amb,day);
  float sh=1.;vec3 sq=L.xyz/L.w*.5+.5;if(sq.x>0.&&sq.x<1.&&sq.y>0.&&sq.y<1.&&sq.z>0.&&sq.z<1.)sh=step(sq.z-.002,depthAt(sq.xy));
  vec3 light=base*(amb+mix(vec3(.22,.25,.38),vec3(.72,.69,.63),day)*nl*(.18+.82*sh));
  if(surface>7.5&&surface<8.5){float fresnel=.07+.52*pow(1.-nv,4.);light=mix(light,vec3(.43,.50,.54)*(.4+.6*day),fresnel);}
  float em=K.x<0.?(-K.x)*(1.-day):K.x;
  if(em>0.)light=mix(light,base*(.9+.25*day),clamp(em,0.,1.));light*=1.13;gl_FragColor=vec4(display(light/(vec3(1.)+light*.14)),alpha);return;
 }

 // Subtle material-scale variation, faded at distance to avoid crawling noise.
 float detailFade=1.-smoothstep(6.,22.,length(eye-P));
 if(surface>3.5&&surface<4.5){float grain=sin(P.x*36.+sin(P.z*1.7)*1.8+sin(P.x*7.)*.20);float pore=sin(P.x*107.+P.z*.7);base*=.965+detailFade*(.025*grain+.009*pore);rough=clamp(rough+grain*.025,.12,1.);}
 if(surface>1.5&&surface<2.5){float weave=sin(P.x*115.)*sin((P.y+P.z)*131.);base*=.988+.012*weave*detailFade;}
 if(surface>8.5){float mineral=noise3(floor(P*140.));base*=.975+.04*mineral*detailFade;}
 float a=rough*rough,a2=a*a,den=nh*nh*(a2-1.)+1.,D=a2/(3.14159265*den*den+.0001),k=(rough+1.)*(rough+1.)/8.;
 float G=(nv/(nv*(1.-k)+k))*(nl/(nl*(1.-k)+k));
 vec3 f0=mix(vec3(.04),base,metal),F=f0+(1.-f0)*pow(1.-vh,5.);
 vec3 spec=min(vec3(3.),D*G*F/max(4.*nv*nl,.001));
 float vis=visibility(n,l),hemi=n.y*.5+.5;
 vec3 skyAmbient=mix(vec3(.23,.215,.20),vec3(.50,.55,.60),hemi);
 // Warm floor bounce and neutral daylight keep white desks and skin from
 // inheriting the exterior's blue sky cast. Night and city lighting stay intact.
 vec3 indoorAmbient=mix(vec3(.24,.225,.21),vec3(.59,.60,.58),hemi);
 vec3 ambient=mix(skyAmbient*vec3(.21,.26,.43),K.w>15.5?skyAmbient:indoorAmbient,day);
 vec3 key=mix(vec3(.45,.52,.85),vec3(1.74,1.65,1.49),day);
 // Modest ambient attenuation inside cast shadows gives furniture contact and
 // recesses depth without introducing invented screen-space occlusion.
 vec3 light=base*ambient*(1.-metal*.68)*(.82+.18*vis)+(base*(1.-metal)*(1.-F)/3.14159265+spec)*key*nl*(.14+.86*vis);
 float rim=pow(1.-nv,3.)*max(dot(n,normalize(vec3(.65,.3,-.6))),0.);
 light+=base*rim*.065+f0*(.12+.25*pow(1.-nv,4.))*(.45+.55*day);
 if(surface>7.5&&surface<8.5){float fresnel=.06+.56*pow(1.-nv,4.);vec3 reflection=mix(vec3(.21,.25,.29),vec3(.57,.62,.64),smoothstep(-.4,.8,reflect(-v,n).y));light=mix(light,reflection*(.32+.68*day),fresnel);}
 if(surface>.5&&surface<1.5)light+=base*vec3(.17,.055,.026)*pow(max(dot(-n,l),0.),2.)*(.5+.5*day);
 float emission=K.x<0.?(-K.x)*(1.-day):K.x;
 if(emission>0.)light=mix(light,base*(.9+.25*day),clamp(emission,0.,1.));
 // Exposure and a gentle shoulder; no claim of HDR environment reflections.
 light*=1.13;light=light/(vec3(1.)+light*.14);gl_FragColor=vec4(display(light),alpha);
}`;
const staticVS=`attribute vec3 pos;attribute vec3 normal;attribute vec4 rgba;attribute vec4 material;uniform mat4 vp;uniform mat4 light;${varying}void main(){P=pos;N=normal;C=rgba.rgb;A=rgba.a;K=material;U=pos.xz;L=light*vec4(pos,1.);gl_Position=vp*vec4(pos,1.);}`;
const instanceVS=`attribute vec3 pos;attribute vec3 normal;attribute vec4 row0;attribute vec4 row1;attribute vec4 row2;attribute vec4 rgba;attribute vec4 material;uniform mat4 vp;uniform mat4 light;${varying}
void main(){vec4 p=vec4(pos,1.);P=vec3(dot(row0,p),dot(row1,p),dot(row2,p));vec3 x=vec3(row0.x,row1.x,row2.x),y=vec3(row0.y,row1.y,row2.y),z=vec3(row0.z,row1.z,row2.z);N=x*normal.x/max(dot(x,x),.000001)+y*normal.y/max(dot(y,y),.000001)+z*normal.z/max(dot(z,z),.000001);C=rgba.rgb;A=rgba.a;K=material;U=pos.xz;L=light*vec4(P,1.);gl_Position=vp*vec4(P,1.);}`;
const shadowFS=`precision highp float;void main(){vec4 v=fract(gl_FragCoord.z*vec4(16777216.,65536.,256.,1.));gl_FragColor=v-v.xxyz*vec4(0.,1./256.,1./256.,1./256.);}`;
type Group={key:string;buffer:WebGLBuffer;count:number;array:Float32Array};
export class OfficeRenderer{
 gl:WebGLRenderingContext;vp=identity();meshes=new Map<string,Float32Array>();yaw=officeOverview().yaw;pitch=officeOverview().pitch;zoom=officeOverview().zoom;target:V3=officeOverview().target;focusPoint:V3|null=null;cameraInsets?:CameraInsets;background?:number;day=true;disposed=false;software=false;quality:RenderQuality='balanced';
 readonly supportsInstancing:boolean;stats={dynamicParts:0,dynamicTriangles:0,staticTriangles:0,drawCalls:0,frameCpuMs:0,shadowSize:1024};
 private inst:ANGLE_instanced_arrays|null;private program:WebGLProgram;private instanceProgram:WebGLProgram|null;private shadowProgram:WebGLProgram;private shadowInstance:WebGLProgram|null;
 private fixed:WebGLBuffer;private fixedShadow:WebGLBuffer;private fixedShadowCount=0;private dynamic:WebGLBuffer;private glass:WebGLBuffer;private fixedCount=0;private glassCount=0;private fb:WebGLFramebuffer;private tex:WebGLTexture;private depth:WebGLRenderbuffer;private light:M4;private dirty=true;private shadowStamp=0;
 private geom=new Map<string,{buffer:WebGLBuffer;count:number}>();private groups=new Map<string,Group>();private locations=new Map<WebGLProgram,Map<string,number|WebGLUniformLocation|null>>();private shadowSize=1024;
 constructor(readonly canvas:HTMLCanvasElement){
  const gl=canvas.getContext('webgl',{antialias:true,alpha:false});if(!gl)throw Error('WebGL不可用；任务看板仍可使用。');this.gl=gl;
  const ext=gl.getExtension('WEBGL_debug_renderer_info');this.software=!!ext&&/swiftshader|llvmpipe/i.test(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL));
  this.inst=gl.getExtension('ANGLE_instanced_arrays');this.supportsInstancing=!!this.inst;
  const program=(vs:string,frag:string)=>{const p=gl.createProgram()!;for(const [type,src]of[[gl.VERTEX_SHADER,vs],[gl.FRAGMENT_SHADER,frag]] as [number,string][]){const s=gl.createShader(type)!;gl.shaderSource(s,src);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS)){const err=gl.getShaderInfoLog(s);gl.deleteShader(s);gl.deleteProgram(p);throw Error(err||'Shader error');}gl.attachShader(p,s);gl.deleteShader(s);}gl.linkProgram(p);if(!gl.getProgramParameter(p,gl.LINK_STATUS))throw Error(gl.getProgramInfoLog(p)||'Shader link error');return p;};
  this.program=program(staticVS,fs);this.instanceProgram=this.inst?program(instanceVS,fs):null;
  this.shadowProgram=program('attribute vec3 pos;uniform mat4 vp;void main(){gl_Position=vp*vec4(pos,1.);}',shadowFS);
  this.shadowInstance=this.inst?program('attribute vec3 pos;attribute vec4 row0;attribute vec4 row1;attribute vec4 row2;uniform mat4 vp;void main(){vec4 p=vec4(pos,1.);gl_Position=vp*vec4(dot(row0,p),dot(row1,p),dot(row2,p),1.);}',shadowFS):null;
  this.fixed=gl.createBuffer()!;this.fixedShadow=gl.createBuffer()!;this.dynamic=gl.createBuffer()!;this.glass=gl.createBuffer()!;this.tex=gl.createTexture()!;this.fb=gl.createFramebuffer()!;this.depth=gl.createRenderbuffer()!;this.allocateShadow();
  this.light=multiply(ortho(49,42),lookAt([-25,38,20],[0,0,0]));this.meshes=modelMeshes();this.meshes.set('cube',cubeVertices());gl.enable(gl.DEPTH_TEST);gl.enable(gl.CULL_FACE);
 }
 private allocateShadow(){const gl=this.gl;gl.bindTexture(gl.TEXTURE_2D,this.tex);gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,this.shadowSize,this.shadowSize,0,gl.RGBA,gl.UNSIGNED_BYTE,null);for(const [p,v]of[[gl.TEXTURE_MIN_FILTER,gl.NEAREST],[gl.TEXTURE_MAG_FILTER,gl.NEAREST],[gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE],[gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE]])gl.texParameteri(gl.TEXTURE_2D,p,v);gl.bindFramebuffer(gl.FRAMEBUFFER,this.fb);gl.framebufferTexture2D(gl.FRAMEBUFFER,gl.COLOR_ATTACHMENT0,gl.TEXTURE_2D,this.tex,0);gl.bindRenderbuffer(gl.RENDERBUFFER,this.depth);gl.renderbufferStorage(gl.RENDERBUFFER,gl.DEPTH_COMPONENT16,this.shadowSize,this.shadowSize);gl.framebufferRenderbuffer(gl.FRAMEBUFFER,gl.DEPTH_ATTACHMENT,gl.RENDERBUFFER,this.depth);if(gl.checkFramebufferStatus(gl.FRAMEBUFFER)!==gl.FRAMEBUFFER_COMPLETE)throw Error('阴影缓冲创建失败');gl.bindFramebuffer(gl.FRAMEBUFFER,null);this.dirty=true;this.stats.shadowSize=this.shadowSize;}
 setQuality(q:RenderQuality){if(!['detail','balanced','economy'].includes(q))return;this.quality=q;const size=q==='detail'?2048:q==='economy'?768:1024;if(size!==this.shadowSize){this.shadowSize=size;this.allocateShadow();}}
 mesh(key:string,v:Float32Array){const old=this.geom.get(key);if(old){this.gl.deleteBuffer(old.buffer);this.geom.delete(key);}this.meshes.set(key,v);this.dirty=true;}
 private resolve(key:string):Float32Array|undefined{let v=this.meshes.get(key);if(!v&&key.startsWith('bevel:')){const n=key.slice(6).split(':').map(Number);if(n.length===4){v=bevelBox(n[0],n[1],n[2],n[3],Math.min(n[0],n[1],n[2])<.075||Math.max(n[0],n[1],n[2])<.7?1:2);this.meshes.set(key,v);}}return v;}
 private material(p:Part):[number,number,number,number]{const surf=p.surface||'matte',d=defaults[surf];return[p.nightEmissive? -p.nightEmissive : p.emissive??0,p.roughness??d[0],p.metalness??d[1],SURF[surf]+(p.layer==='environment'?16:0)];}
 private bake(parts:Part[]):Float32Array{let count=0;for(const p of parts)count+=(this.resolve(p.mesh||'cube')?.length||0)/6;const out=new Float32Array(count*14);let k=0;
  for(const p of parts){const src=this.resolve(p.mesh||'cube');if(!src)continue;const m=p.matrix||modelMatrix(p.p,p.s,p.yaw),sx=m[0]**2+m[1]**2+m[2]**2||1,sy=m[4]**2+m[5]**2+m[6]**2||1,sz=m[8]**2+m[9]**2+m[10]**2||1,mat=this.material(p);
   for(let i=0;i<src.length;i+=6){const x=src[i],y=src[i+1],z=src[i+2],nx=src[i+3]/sx,ny=src[i+4]/sy,nz=src[i+5]/sz;out[k++]=m[0]*x+m[4]*y+m[8]*z+m[12];out[k++]=m[1]*x+m[5]*y+m[9]*z+m[13];out[k++]=m[2]*x+m[6]*y+m[10]*z+m[14];out[k++]=m[0]*nx+m[4]*ny+m[8]*nz;out[k++]=m[1]*nx+m[5]*ny+m[9]*nz;out[k++]=m[2]*nx+m[6]*ny+m[10]*nz;out.set([...p.color,p.alpha??1,...mat],k);k+=8;}
  }return out;
 }
 private location(p:WebGLProgram,name:string,attr=false):any{let m=this.locations.get(p);if(!m){m=new Map();this.locations.set(p,m);}const key=(attr?'a:':'u:')+name;if(!m.has(key))m.set(key,attr?this.gl.getAttribLocation(p,name):this.gl.getUniformLocation(p,name));return m.get(key);}
 private clearAttrs(){for(let i=0;i<8;i++){this.gl.disableVertexAttribArray(i);this.inst?.vertexAttribDivisorANGLE(i,0);}}
 private attrib(p:WebGLProgram,name:string,n:number,stride:number,offset:number,divisor=0){const a=this.location(p,name,true);if(a<0)return;this.gl.enableVertexAttribArray(a);this.gl.vertexAttribPointer(a,n,this.gl.FLOAT,false,stride,offset);this.inst?.vertexAttribDivisorANGLE(a,divisor);}
 private bindStatic(p:WebGLProgram,b:WebGLBuffer,shadow=false){this.clearAttrs();this.gl.bindBuffer(this.gl.ARRAY_BUFFER,b);this.attrib(p,'pos',3,56,0);if(!shadow){this.attrib(p,'normal',3,56,12);this.attrib(p,'rgba',4,56,24);this.attrib(p,'material',4,56,40);}}
 private prepareGroups(parts:Part[],prefix:string):Group[]{const byMesh=new Map<string,Part[]>();for(const p of parts){const key=p.mesh||'cube';if(!this.resolve(key))continue;if(!byMesh.has(key))byMesh.set(key,[]);byMesh.get(key)!.push(p);}const result:Group[]=[];
  for(const [key,list]of byMesh){let g=this.groups.get(prefix+key);if(!g){g={key,buffer:this.gl.createBuffer()!,count:0,array:new Float32Array(0)};this.groups.set(prefix+key,g);}g.count=list.length;if(g.array.length<list.length*20)g.array=new Float32Array(list.length*24);let i=0;for(const p of list){const m=p.matrix||modelMatrix(p.p,p.s,p.yaw);g.array.set([m[0],m[4],m[8],m[12],m[1],m[5],m[9],m[13],m[2],m[6],m[10],m[14],...p.color,p.alpha??1,...this.material(p)],i);i+=20;}this.gl.bindBuffer(this.gl.ARRAY_BUFFER,g.buffer);this.gl.bufferData(this.gl.ARRAY_BUFFER,g.array.subarray(0,list.length*20),this.gl.DYNAMIC_DRAW);result.push(g);}return result;
 }
 private drawGroups(p:WebGLProgram,groups:Group[],shadow=false){const gl=this.gl;this.clearAttrs();for(const g of groups){let geom=this.geom.get(g.key);if(!geom){const v=this.resolve(g.key)!;geom={buffer:gl.createBuffer()!,count:v.length/6};gl.bindBuffer(gl.ARRAY_BUFFER,geom.buffer);gl.bufferData(gl.ARRAY_BUFFER,v,gl.STATIC_DRAW);this.geom.set(g.key,geom);}gl.bindBuffer(gl.ARRAY_BUFFER,geom.buffer);this.attrib(p,'pos',3,24,0);if(!shadow)this.attrib(p,'normal',3,24,12);gl.bindBuffer(gl.ARRAY_BUFFER,g.buffer);this.attrib(p,'row0',4,80,0,1);this.attrib(p,'row1',4,80,16,1);this.attrib(p,'row2',4,80,32,1);if(!shadow){this.attrib(p,'rgba',4,80,48,1);this.attrib(p,'material',4,80,64,1);}this.inst!.drawArraysInstancedANGLE(gl.TRIANGLES,0,geom.count,g.count);this.stats.drawCalls++;}}
 setStatic(parts:Part[]){const gl=this.gl,a=this.bake(parts.filter(p=>(p.alpha??1)>=1)),b=this.bake(parts.filter(p=>(p.alpha??1)<1));gl.bindBuffer(gl.ARRAY_BUFFER,this.fixed);gl.bufferData(gl.ARRAY_BUFFER,a,gl.STATIC_DRAW);this.fixedCount=a.length/14;gl.bindBuffer(gl.ARRAY_BUFFER,this.glass);gl.bufferData(gl.ARRAY_BUFFER,b,gl.STATIC_DRAW);this.glassCount=b.length/14;this.stats.staticTriangles=(this.fixedCount+this.glassCount)/3;const casters=this.bake(parts.filter(p=>(p.alpha??1)>=1&&p.castShadow!==false&&(p.matrix? p.matrix[13]>.16 : p.p[1]>.16)));gl.bindBuffer(gl.ARRAY_BUFFER,this.fixedShadow);gl.bufferData(gl.ARRAY_BUFFER,casters,gl.STATIC_DRAW);this.fixedShadowCount=casters.length/14;this.dirty=true;}
 reset(){Object.assign(this,officeOverview());this.focusPoint=null;}
 draw(parts:Part[]){if(this.disposed)return;const begin=performance.now(),gl=this.gl,c=this.canvas,w=Math.max(c.clientWidth,1),h=Math.max(c.clientHeight,1),pixels=this.quality==='detail'?1_800_000:this.quality==='economy'?520_000:1_050_000,cap=this.quality==='detail'?2:this.quality==='economy'?1:1.5,scale=Math.min(devicePixelRatio||1,this.software?Math.sqrt(pixels/(w*h)):cap);if(c.width!==Math.round(w*scale)||c.height!==Math.round(h*scale)){c.width=Math.round(w*scale);c.height=Math.round(h*scale);}
  this.stats.drawCalls=0;this.stats.dynamicParts=parts.length;this.stats.dynamicTriangles=parts.reduce((n,p)=>n+(this.resolve(p.mesh||'cube')?.length||0)/18,0);
  const opaque=parts.filter(p=>(p.alpha??1)>=1&&p.surface!=='shadow'),transparent=parts.filter(p=>(p.alpha??1)<1||p.surface==='shadow');let groups:Group[]=[],glassGroups:Group[]=[],bakedCount=0;
  if(this.inst){groups=this.prepareGroups(opaque,'o:');glassGroups=this.prepareGroups(transparent,'t:');}else{const v=this.bake(opaque);gl.bindBuffer(gl.ARRAY_BUFFER,this.dynamic);gl.bufferData(gl.ARRAY_BUFFER,v,gl.DYNAMIC_DRAW);bakedCount=v.length/14;}
  gl.depthMask(true);gl.disable(gl.BLEND);const now=performance.now();if(this.dirty||now-this.shadowStamp>(this.quality==='economy'?500:this.software?220:70)){
   gl.bindFramebuffer(gl.FRAMEBUFFER,this.fb);gl.viewport(0,0,this.shadowSize,this.shadowSize);gl.clearColor(1,1,1,1);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);gl.useProgram(this.shadowProgram);gl.uniformMatrix4fv(this.location(this.shadowProgram,'vp'),false,this.light);this.bindStatic(this.shadowProgram,this.fixedShadow,true);gl.drawArrays(gl.TRIANGLES,0,this.fixedShadowCount);
   if(this.shadowInstance){gl.useProgram(this.shadowInstance);gl.uniformMatrix4fv(this.location(this.shadowInstance,'vp'),false,this.light);this.drawGroups(this.shadowInstance,groups,true);}else{this.bindStatic(this.shadowProgram,this.dynamic,true);gl.drawArrays(gl.TRIANGLES,0,bakedCount);}this.dirty=false;this.shadowStamp=now;
  }
  gl.bindFramebuffer(gl.FRAMEBUFFER,null);gl.viewport(0,0,c.width,c.height);gl.clearColor(...color(this.background??(this.day?0xe6e9ed:0x111b2a)),1);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
  const {vp,eye}=officeProjection(this,w,h,this.cameraInsets,!!this.focusPoint);this.vp=vp;
  const uniforms=(p:WebGLProgram,focusPass=-1)=>{gl.useProgram(p);gl.uniformMatrix4fv(this.location(p,'vp'),false,this.vp);gl.uniformMatrix4fv(this.location(p,'light'),false,this.light);gl.uniform1f(this.location(p,'day'),this.day?1:0);gl.uniform3fv(this.location(p,'eye'),eye);gl.uniform3fv(this.location(p,'focusPoint'),this.focusPoint||this.target);gl.uniform1f(this.location(p,'focusPass'),focusPass);gl.uniform1f(this.location(p,'shadowSize'),this.shadowSize);gl.uniform1f(this.location(p,'environmentQuality'),this.quality==='detail'?2:this.quality==='economy'?0:1);gl.uniform1f(this.location(p,'softShadows'),this.quality==='economy'?0:this.quality==='detail'?2:1);gl.activeTexture(gl.TEXTURE0);gl.bindTexture(gl.TEXTURE_2D,this.tex);gl.uniform1i(this.location(p,'shadow'),0);};
  uniforms(this.program,this.focusPoint?0:-1);this.bindStatic(this.program,this.fixed);gl.drawArrays(gl.TRIANGLES,0,this.fixedCount);this.stats.drawCalls++;
  if(this.instanceProgram){uniforms(this.instanceProgram);this.drawGroups(this.instanceProgram,groups);}else{uniforms(this.program);this.bindStatic(this.program,this.dynamic);gl.drawArrays(gl.TRIANGLES,0,bakedCount);}
  gl.enable(gl.BLEND);gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);gl.depthMask(false);
  // Composite only the soft foreground opening after employees are drawn.
  // Static buffers are never radius-clipped or rebuilt while a person moves.
  if(this.focusPoint){uniforms(this.program,1);this.bindStatic(this.program,this.fixed);gl.drawArrays(gl.TRIANGLES,0,this.fixedCount);this.stats.drawCalls++;}
  uniforms(this.program);
  if(this.instanceProgram){uniforms(this.instanceProgram);this.drawGroups(this.instanceProgram,glassGroups);}else if(transparent.length){const v=this.bake(transparent);gl.bindBuffer(gl.ARRAY_BUFFER,this.dynamic);gl.bufferData(gl.ARRAY_BUFFER,v,gl.DYNAMIC_DRAW);this.bindStatic(this.program,this.dynamic);gl.drawArrays(gl.TRIANGLES,0,v.length/14);}
  uniforms(this.program);gl.disable(gl.CULL_FACE);this.bindStatic(this.program,this.glass);gl.drawArrays(gl.TRIANGLES,0,this.glassCount);gl.enable(gl.CULL_FACE);gl.depthMask(true);gl.disable(gl.BLEND);this.clearAttrs();this.stats.frameCpuMs=performance.now()-begin;
 }
 project(p:V3):V3{const n=transform(this.vp,p);return[(n[0]+1)*this.canvas.clientWidth/2,(1-n[1])*this.canvas.clientHeight/2,n[2]];}
 dispose(){if(this.disposed)return;this.disposed=true;const gl=this.gl;for(const b of[this.fixed,this.fixedShadow,this.dynamic,this.glass,...Array.from(this.geom.values(),x=>x.buffer),...Array.from(this.groups.values(),x=>x.buffer)])gl.deleteBuffer(b);for(const p of[this.program,this.shadowProgram,this.instanceProgram,this.shadowInstance])if(p)gl.deleteProgram(p);gl.deleteTexture(this.tex);gl.deleteRenderbuffer(this.depth);gl.deleteFramebuffer(this.fb);this.groups.clear();this.geom.clear();this.meshes.clear();this.locations.clear();}
}
