import puppeteer from 'puppeteer';
import {mkdir, readFile, readdir, rm, writeFile} from 'node:fs/promises';
import {join} from 'node:path';

const VENUES='函館 青森 いわき平 弥彦 前橋 取手 宇都宮 大宮 西武園 京王閣 立川 松戸 千葉 川崎 平塚 小田原 伊東 静岡 名古屋 岐阜 大垣 豊橋 富山 松阪 四日市 福井 奈良 向日町 和歌山 岸和田 玉野 広島 防府 高松 小松島 高知 松山 小倉 久留米 武雄 佐世保 別府 熊本'.split(' ');
const date=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Tokyo',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
const dataRoot=join(process.cwd(),'data');
const directory=join(dataRoot,date);
const manifestPath=join(directory,'manifest.json');
await mkdir(dataRoot,{recursive:true});
for(const entry of await readdir(dataRoot,{withFileTypes:true})){
  if(entry.isDirectory()&&/^\d{4}-\d{2}-\d{2}$/.test(entry.name)&&entry.name!==date){
    await rm(join(dataRoot,entry.name),{recursive:true,force:true});
  }
}
await mkdir(directory,{recursive:true});
let prior={};
try{prior=JSON.parse(await readFile(manifestPath,'utf8'))}catch{}
const manifest={date,updatedAt:new Date().toISOString(),status:'running',races:Array.isArray(prior.races)?prior.races:[],errors:[],completedVenues:[]};
const saveManifest=()=>{
  manifest.races.sort((a,b)=>a.venue.localeCompare(b.venue,'ja')||Number(a.race)-Number(b.race));
  return writeFile(manifestPath,JSON.stringify(manifest,null,2)+'\n');
};
await saveManifest();

// These functions execute in the KEIRIN.JP page. The line grouping comes
// from the official race list; it is never inferred from rider names.
function extractRace(race){
  // KEIRIN.JP uses sltbl_02 for 1R and sltbl_02-2 for later races.
  const cards=[...document.querySelectorAll('#sldivSyusouList > table.sltbl_02, #sldivSyusouList > table.sltbl_02-2')];
  const card=cards.find(t=>new RegExp(`^\\s*${race}R(?:\\s|　)`).test(t.innerText));
  if(!card)throw Error('出走表なし');
  const riders=[...card.querySelectorAll('a.sllink_name')].map(a=>{
    let cell=a.closest('td'),number=null;
    for(let i=0;i<4&&cell;i++,cell=cell.previousElementSibling){
      if(/^sltb-no\d/.test(cell.className)){number=Number(cell.textContent.trim());break}
    }
    return {number,name:a.textContent.replace(/\s+/g,' ').trim(),snum:a.getAttribute('onclick')?.match(/\d{6}/)?.[0]||null};
  }).filter(x=>x.number>=1&&x.number<=9&&x.snum);
  const row=card.querySelector(`[id^="slyoso_td_${race}_"]`)?.closest('tr');
  const cells=row?[...row.querySelectorAll('td')].map(td=>Number(td.textContent.trim())||0):[];
  const lines=[],seen=new Set();let group=[];
  for(const n of cells){
    if(n){if(!seen.has(n)){group.push(n);seen.add(n)}}
    else if(group.length){lines.push(group);group=[]}
  }
  if(group.length)lines.push(group);
  return {riders,lines:riders.length>=5&&seen.size===riders.length&&riders.every(r=>seen.has(r.number))?lines:[]};
}
function extractScore(){
  const table=[...document.querySelectorAll('table')].find(t=>{
    const first=t.querySelector('tr');
    return first&&[...first.children].some(c=>c.textContent.trim()==='競走得点')&&first.children.length>8;
  });
  if(!table)return null;
  const headers=[...table.querySelector('tr').children];
  const i=headers.findIndex(c=>c.textContent.trim()==='競走得点');
  const score=table.querySelectorAll('tr')[1]?.children[i]?.textContent.trim();
  return /^\d{2,3}\.\d{1,2}$/.test(score||'')?score:null;
}
const norm=s=>s.replace(/[\s　]/g,'');
async function discover(page){
  await page.goto('https://keirin.jp/pc/top',{waitUntil:'domcontentloaded',timeout:30000});
  await page.waitForSelector('button',{timeout:15000});
  return page.evaluate(known=>{
    const norm=s=>s.replace(/[\s　]/g,'');
    return [...new Set([...document.querySelectorAll('tr')].filter(row=>[...row.querySelectorAll('button')].some(b=>norm(b.textContent).includes('出走表一覧'))).map(row=>{
      const value=norm(row.querySelector('td')?.textContent||'');
      return known.find(v=>value.startsWith(v));
    }).filter(Boolean))];
  },VENUES);
}
async function openRaceList(page,venue){
  await page.goto('https://keirin.jp/pc/top',{waitUntil:'domcontentloaded',timeout:30000});
  await page.waitForFunction(v=>[...document.querySelectorAll('tr')].some(row=>row.querySelector('td')?.textContent.replace(/[\s　]/g,'').startsWith(v)&&[...row.querySelectorAll('button')].some(b=>b.textContent.replace(/[\s　]/g,'').includes('出走表一覧'))),{timeout:15000},venue);
  await page.evaluate(v=>{
    const row=[...document.querySelectorAll('tr')].find(r=>r.querySelector('td')?.textContent.replace(/[\s　]/g,'').startsWith(v)&&[...r.querySelectorAll('button')].some(b=>b.textContent.replace(/[\s　]/g,'').includes('出走表一覧')));
    [...row.querySelectorAll('button')].find(b=>b.textContent.replace(/[\s　]/g,'').includes('出走表一覧')).click();
  },venue);
  await page.waitForFunction(()=>location.pathname==='/pc/racelist'&&document.querySelectorAll('#sldivSyusouList > table.sltbl_02, #sldivSyusouList > table.sltbl_02-2').length>0,{timeout:20000});
}
async function scoresFor(browser,riders,scores){
  const queue=[...new Set(riders.map(r=>r.snum).filter(id=>!scores.has(id)))];
  let next=0;
  await Promise.all(Array.from({length:Math.min(4,queue.length)},async()=>{
    const page=await browser.newPage();
    try{
      while(next<queue.length){
        const snum=queue[next++];
        try{
          await page.goto(`https://keirin.jp/pc/racerprofile?snum=${snum}`,{waitUntil:'domcontentloaded',timeout:15000});
          await page.waitForFunction(()=>[...document.querySelectorAll('td')].some(c=>c.textContent.trim()==='競走得点'),{timeout:7000});
          scores.set(snum,await page.evaluate(extractScore));
        }catch(e){scores.set(snum,null);console.warn(`競走得点を確認できません: ${snum}: ${e.message}`)}
      }
    }finally{await page.close()}
  }));
}
let browser;
try{
  browser=await puppeteer.launch({headless:true,args:['--no-sandbox']});
  const page=await browser.newPage();
  try{
    const venues=await discover(page);
    manifest.discoveredVenues=venues;
    await saveManifest();
    for(const venue of venues){
      try{
        await openRaceList(page,venue);
        const cards=[];
        for(let race=1;race<=12;race++){
          try{
            const card=await page.evaluate(extractRace,race);
            if([5,6,7,8,9].includes(card.riders.length))cards.push({race,...card});
          }catch{} // Some meetings have fewer than 12 races.
        }
        if(!cards.length)throw Error('出走表を読めませんでした');
        const scores=new Map();
        for(const card of cards){
          if(manifest.races.some(x=>x.venue===venue&&Number(x.race)===card.race))continue;
          await scoresFor(browser,card.riders,scores);
          const riders=card.riders.map(({snum,...r})=>({...r,score:scores.get(snum)||null}));
          const data={date,venue,race:card.race,riders,lines:card.lines,source:'KEIRIN.JP',lineSource:card.lines.length?'KEIRIN.JP':null,scoreType:'直近4ヶ月の競走得点',updatedAt:new Date().toISOString()};
          await writeFile(join(directory,`${venue}-${card.race}.json`),JSON.stringify(data)+'\n');
          manifest.races.push({venue,race:card.race});
          manifest.updatedAt=new Date().toISOString();
          await saveManifest();
          console.log(`${venue} ${card.race}R: ${riders.length}人`);
        }
        manifest.completedVenues.push(venue);
      }catch(e){manifest.errors.push({venue,message:String(e.message||e).slice(0,200)})}
      await saveManifest();
    }
    manifest.status=venues.length===0?'no-meetings':manifest.errors.length?'partial':'complete';
  }finally{await page.close()}
}catch(e){manifest.errors.push({message:String(e.message||e).slice(0,200)});manifest.status=manifest.races.length?'partial':'failed'}
finally{if(browser)await browser.close();manifest.updatedAt=new Date().toISOString();await saveManifest()}
console.log(`終了: ${manifest.status} / ${manifest.races.length}レース / ${manifest.errors.length}エラー`);
if(manifest.status==='failed'||(manifest.status==='partial'&&!manifest.races.length))process.exitCode=1;
