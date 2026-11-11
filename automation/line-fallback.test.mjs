import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,writeFile,readFile,rm} from 'node:fs/promises';
import {join} from 'node:path';
import {fillMissingLines,mergeMissingLines} from './fill-lines.mjs';
import {resolveTargetDate} from './target-date.mjs';
import {extractSupplementLines} from './parsers.mjs';
const riders=Array.from({length:5},(_,i)=>({number:i+1,snum:String(i+1).padStart(6,'0')}));
const lines=[[1,2],[3,4,5]], saved={date:'2026-11-06',venue:'青森',race:1,raceId:'202611061201',riders,lines:[]};
test('netkeirin observed DOM structure preserves groups and order',()=>{
 const previous=globalThis.document;
 const boxes=[1,2,null,3,4,5].map(n=>({querySelector:selector=>selector==='.Shaban_Num'?(n?{textContent:String(n)}:null):n?null:{}}));
 globalThis.document={querySelector:selector=>selector==='link[rel="canonical"]'?{href:'https://keirin.netkeiba.com/race/entry/?race_id=202611061201'}:{querySelectorAll:()=>boxes}};
 try{assert.deepEqual(extractSupplementLines(saved.raceId),lines);assert.throws(()=>extractSupplementLines('202611061202'))}finally{globalThis.document=previous}
});
test('full acquisition retains date guard',async()=>{
 const source=await readFile(new URL('./scrape.mjs',import.meta.url),'utf8');
 assert.match(source,/if\(!options\['lines-only'\]&&date!==currentDate\)/);
 assert.match(source,/fallbackLines=await detail.evaluate\(extractSupplementLines,raceId\)/);
});

test('evidence callback is initialized before the lines-only entry point',async()=>{
 const source=await readFile(new URL('./scrape.mjs',import.meta.url),'utf8');
 assert.ok(source.indexOf('const evidence=')<source.indexOf("if(options['lines-only'])"));
});

test('official failure followed by verified fallback is successful and retains warning',async()=>{
 const directory=await mkdtemp(join(process.cwd(),'.line-test-'));
 try{
  await writeFile(join(directory,'青森-1.json'),JSON.stringify(saved));
  const report=await fillMissingLines({directory,date:saved.date,manifest:{date:saved.date,races:[{venue:'青森',race:1}]},getCard:async()=>{throw Error('historical official page unavailable')},getFallback:async()=>({...saved,lines,lineSource:'netkeirin'})});
  assert.equal(report.status,'complete');assert.equal(report.errors.length,0);assert.equal(report.sourceWarnings.length,1);assert.equal(report.added.length,1);
 }finally{await rm(directory,{recursive:true,force:true})}
});
test('official lines win',()=>assert.equal(mergeMissingLines(saved,{riders,lines},'now').data.lineSource,'KEIRIN.JP'));

for(const message of ['HTTP 403','HTTP 429','CAPTCHA'])test(`access restriction stops the batch: ${message}`,async()=>{
 const directory=await mkdtemp(join(process.cwd(),'.line-test-'));let calls=0;
 try{
  await writeFile(join(directory,'青森-1.json'),JSON.stringify(saved));
  await writeFile(join(directory,'青森-2.json'),JSON.stringify({...saved,race:2,raceId:'202611061202'}));
  const report=await fillMissingLines({directory,date:saved.date,manifest:{date:saved.date,races:[{venue:'青森',race:1},{venue:'青森',race:2}]},getCard:async()=>{calls++;throw Error(message)},getFallback:async()=>{throw Error('fallback must not run')}});
  assert.equal(calls,1);assert.equal(report.accessStopped,true);assert.equal(report.errors.length,1);assert.equal(report.added.length,0);
 }finally{await rm(directory,{recursive:true,force:true})}
});
test('netkeirin provenance',()=>assert.equal(mergeMissingLines(saved,{riders,lines,lineSource:'netkeirin'},'now').data.lineSource,'netkeirin'));
test('existing lines preserved',()=>assert.equal(mergeMissingLines({...saved,lines},null,'now').reason,'preserved'));
test('roster mismatch rejected',()=>assert.equal(mergeMissingLines(saved,{riders:[],lines},'now').reason,'roster-mismatch'));
test('invalid line set rejected',()=>assert.equal(mergeMissingLines(saved,{riders,lines:[[1,1,2,3,4]]},'now').reason,'invalid-lines'));
test('delayed evening keeps intended date',()=>assert.equal(resolveTargetDate({now:'2026-11-06T16:00:00Z',schedule:'0 9 * * *'}),'2026-11-06'));
for(const mode of ['official','fallback','failed','preserved'])test(`saved day integration ${mode}`,async()=>{
 const directory=await mkdtemp(join(process.cwd(),'.line-test-'));
 try{
  const original={...saved,lines:mode==='preserved'?lines:[]};
  const manifest={date:saved.date,status:'complete',races:[{venue:'青森',race:1}]};
  await writeFile(join(directory,'青森-1.json'),JSON.stringify(original));
  let fallbackCalls=0;
  const report=await fillMissingLines({directory,date:saved.date,manifest,getCard:async()=>{if(mode==='preserved')throw Error('must not fetch');return mode==='official'?{riders,lines}:null},getFallback:async()=>{fallbackCalls++;if(mode==='failed')throw Error('source unavailable');return {...saved,lines,lineSource:'netkeirin'}}});
  const after=JSON.parse(await readFile(join(directory,'青森-1.json')));
  assert.equal(manifest.status,'complete');
  if(mode==='failed'){assert.deepEqual(after,original);assert.equal(report.errors.length,1)}
  else assert.deepEqual(after.lines,lines);
  if(mode==='official'||mode==='preserved')assert.equal(fallbackCalls,0);
 }finally{await rm(directory,{recursive:true,force:true})}
});
