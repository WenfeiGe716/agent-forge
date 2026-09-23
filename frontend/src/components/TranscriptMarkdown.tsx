import {ReactNode} from 'react';

// A text-only Markdown subset for streamed messages. HTML is always React text;
// links never receive an executable/local URL and remote images never load.
function inline(text:string,depth=0):ReactNode[]{
 if(depth>8)return [text];
 const result:ReactNode[]=[];let pos=0,plain='';
 const flush=()=>{if(plain){result.push(plain);plain='';}};
 while(pos<text.length){
  if(text[pos]==='`'){
   const ticks=text.slice(pos).match(/^`+/)![0],end=text.indexOf(ticks,pos+ticks.length);
   if(end>=0){flush();result.push(<code key={pos}>{text.slice(pos+ticks.length,end)}</code>);pos=end+ticks.length;continue;}
  }
  if(text.startsWith('**',pos)||text.startsWith('__',pos)){
   const marker=text.slice(pos,pos+2),end=text.indexOf(marker,pos+2);
   if(end>pos+2){flush();result.push(<strong key={pos}>{inline(text.slice(pos+2,end),depth+1)}</strong>);pos=end+2;continue;}
  }
  if(text[pos]==='['){
   const labelEnd=text.indexOf('](',pos+1);
   if(labelEnd>pos){let end=labelEnd+2,balance=1;for(;end<text.length;end++){if(text[end]==='(')balance++;if(text[end]===')'&&!--balance)break;}
    if(balance===0){flush();const label=text.slice(pos+1,labelEnd),target=text.slice(labelEnd+2,end).trim();
     if(/^https?:\/\//i.test(target))result.push(<a key={pos} href={target} target="_blank" rel="noopener noreferrer">{inline(label,depth+1)}</a>);
     else result.push(<code key={pos} className="transcript-file-reference" title={target}>{label}</code>);
     pos=end+1;continue;
    }
   }
  }
  plain+=text[pos++];
 }
 flush();return result;
}
const list=/^\s*(?:[-+*]|\d+[.)])\s+(.+)$/;
const heading=/^(#{1,6})\s+(.+)$/;
const fence=/^\s*(`{3,}|~{3,})(.*)$/;
const tableRule=/^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/;
const cells=(line:string)=>line.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>c.trim());
export default function TranscriptMarkdown({text}:{text:string}){
 const lines=text.replace(/\r\n/g,'\n').split('\n'),blocks:ReactNode[]=[];let i=0;
 while(i<lines.length){
  const line=lines[i],key=i;if(!line.trim()){i++;continue;}
  const start=line.match(fence);
  if(start){const code:string[]=[];i++;while(i<lines.length&&!new RegExp('^\\s*'+start[1][0]+'{'+start[1].length+',}\\s*$').test(lines[i]))code.push(lines[i++]);if(i<lines.length)i++;blocks.push(<div key={key} className="transcript-code">{start[2].trim()&&<small>{start[2].trim()}</small>}<pre><code>{code.join('\n')}</code></pre></div>);continue;}
  const h=line.match(heading);
  if(h){blocks.push(h[1].length<3?<h3 key={key}>{inline(h[2])}</h3>:<h4 key={key}>{inline(h[2])}</h4>);i++;continue;}
  if(i+1<lines.length&&line.includes('|')&&tableRule.test(lines[i+1])){
   const headers=cells(line),rows:string[][]=[];i+=2;while(i<lines.length&&lines[i].includes('|')&&lines[i].trim())rows.push(cells(lines[i++]));
   blocks.push(<div key={key} className="transcript-table" tabIndex={0}><table><thead><tr>{headers.map((c,j)=><th key={j}>{inline(c)}</th>)}</tr></thead><tbody>{rows.map((row,r)=><tr key={r}>{row.map((c,j)=><td key={j}>{inline(c)}</td>)}</tr>)}</tbody></table></div>);continue;
  }
  if(list.test(line)){
   const ordered=/^\s*\d+[.)]/.test(line),items:ReactNode[]=[];const startNumber=ordered?parseInt(line.trim(),10):undefined;
   while(i<lines.length&&list.test(lines[i])&&/^\s*\d+[.)]/.test(lines[i])===ordered){items.push(<li key={i}>{inline(lines[i].match(list)![1])}</li>);i++;}
   blocks.push(ordered?<ol key={key} start={startNumber}>{items}</ol>:<ul key={key}>{items}</ul>);continue;
  }
  if(/^>\s?/.test(line)){const quote:string[]=[];while(i<lines.length&&/^>\s?/.test(lines[i]))quote.push(lines[i++].replace(/^>\s?/,''));blocks.push(<blockquote key={key}>{inline(quote.join('\n'))}</blockquote>);continue;}
  if(/^\s*(?:---+|\*\*\*+)\s*$/.test(line)){blocks.push(<hr key={key}/>);i++;continue;}
  const paragraph=[lines[i++]];
  while(i<lines.length&&lines[i].trim()&&!fence.test(lines[i])&&!heading.test(lines[i])&&!list.test(lines[i])&&!/^>/.test(lines[i])&&!(i+1<lines.length&&tableRule.test(lines[i+1])))paragraph.push(lines[i++]);
  blocks.push(<p key={key}>{inline(paragraph.join('\n'))}</p>);
 }
 return <div className="transcript-markdown">{blocks}</div>;
}
