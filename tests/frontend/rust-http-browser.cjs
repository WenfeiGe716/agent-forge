const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const http = require('node:http');
const crypto = require('node:crypto');
const { spawn } = require('node:child_process');
const { chromium } = require(process.env.FORGE_PLAYWRIGHT_MODULE || 'playwright');

async function main() {
  const root = path.resolve(__dirname, '../..');
  const output = path.join(root, '.local/validation', `rust-browser-${Date.now()}`);
  fs.mkdirSync(output, { recursive: true, mode: 0o700 });
  const probe = net.createServer();
  await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
  const port = probe.address().port;
  await new Promise(resolve => probe.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const providerServer=http.createServer((request,response)=>{
    response.writeHead(request.method==='GET'&&request.url==='/v1/models'?200:404,{'content-type':'application/json'});
    response.end(JSON.stringify({data:[{id:'fixture',object:'model'}]}));
  });
  await new Promise(resolve=>providerServer.listen(0,'127.0.0.1',resolve));
  const password = crypto.randomBytes(24).toString('base64url');
  const env = Object.fromEntries(Object.entries(process.env).filter(([name]) => !name.startsWith('FORGE_')));
  Object.assign(env, { ADMIN_PASSWORD: password, ENCRYPTION_KEY: crypto.randomBytes(32).toString('base64url') + '=',
    FORGE_CODEX_BINARY: path.join(root,'rust/crates/forge-runtime/tests/fixtures/execution_app_server.py') });
  const server = spawn(path.join(root,'rust/target/debug/forge-server'), ['--listen',`127.0.0.1:${port}`,
    '--product-db',path.join(output,'product.sqlite3'),'--rust-db',path.join(output,'runs.sqlite3'),
    '--data-root',path.join(output,'data'),'--frontend',path.join(root,'frontend/dist')], { env, stdio: 'ignore' });
  let browser;
  try {
    for (let count=0; count<100; count++) {
      if (server.exitCode !== null) throw new Error('Rust server exited before readiness');
      try { if ((await fetch(base+'/api/health')).ok) break; } catch {}
      await new Promise(resolve => setTimeout(resolve,100));
    }
    let cookie='';
    async function api(url,body) {
      const response=await fetch(base+'/api'+url,{method:'POST',headers:{'content-type':'application/json','x-forge-request':'1',cookie},body:JSON.stringify(body)});
      if (url==='/auth/login') cookie=response.headers.get('set-cookie').split(';')[0];
      const value=await response.json();
      if (!response.ok) throw new Error(`${url}: ${response.status} ${JSON.stringify(value)}`);
      return value;
    }
    await api('/auth/login',{username:'admin',password});
    const provider=await api('/providers',{name:'Local validation',protocol:'openai_responses',base_url:`http://127.0.0.1:${providerServer.address().port}/v1`,api_key:'unused-fixture-key',external:false});
    const model=await api('/models',{name:'Local validation',provider_id:provider.id,model_id:'fixture',parameters:{},allowed_parameters:[],supports_tools:true});
    await api('/agents',{name:'Developer',role:'developer',model_profile_id:model.id});
    await api('/projects',{name:'Validation workspace',blank:true,execution_mode:'local'});
    browser=await chromium.launch({headless:true,channel:process.env.FORGE_BROWSER_CHANNEL || 'chrome'});
    const context=await browser.newContext({viewport:{width:1440,height:1000}});
    const page=await context.newPage();
    const errors=[];
    const failed=[];
    page.on('pageerror',error=>errors.push(error.message));
    page.on('response',response=>{if(response.url().includes('/api/')&&response.status()>=400&&!response.url().endsWith('/auth/me'))failed.push({url:new URL(response.url()).pathname,status:response.status()});});
    await page.goto(base);
    await page.locator('input[autocomplete="current-password"]').fill(password);
    await page.getByRole('button',{name:'登录工作空间'}).click();
    await page.locator('.studio-header').waitFor();
    await page.waitForTimeout(800);
    await page.screenshot({path:path.join(output,'desktop.png'),fullPage:true});
    await page.getByRole('button',{name:'运行设置',exact:true}).click();
    const nav=page.locator('.sidebar nav button');
    const count=await nav.count();
    for(let i=1;i<count;i++) {
      await nav.nth(i).click();
      await page.waitForTimeout(300);
    }
    await nav.nth(0).click();
    await page.setViewportSize({width:390,height:844});
    await page.waitForTimeout(600);
    await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});
    const result={screens:count,errors,failed,output};
    fs.writeFileSync(path.join(output,'verification.json'),JSON.stringify(result,null,2));
    if(errors.length||failed.length) throw new Error(JSON.stringify(result));
    console.log(JSON.stringify(result));
  } finally {
    if(browser)await browser.close();
    server.kill('SIGTERM');
    await new Promise(resolve=>{if(server.exitCode!==null)resolve();else server.once('exit',resolve);});
    await new Promise(resolve=>providerServer.close(resolve));
  }
}
main().catch(error=>{console.error(error.message);process.exitCode=1;});
