import puppeteer from 'puppeteer';
import {mkdir,readFile,writeFile} from 'node:fs/promises';
import {resolve,join} from 'node:path';
import {parseArgs} from 'node:util';
import {extractRace,extractProfile,extractSupplement} from './parsers.mjs';

const {values:options}=parseArgs({options:{venue:{type:'string'},race:{type:'string'},'data-root':{type:'string'},evidence:{type:'string'}}});
if(options.race&&(!options.venue||!/^([1-9]|1[0-2])$/.test(options.race)))throw Error('--race は --venue と1〜12を指定してください');
const VENUES='函館 青森 いわき平 弥彦 前橋 取手 宇都宮 大宮 西武園 京王閣 立川 松戸 千葉 川崎 平塚 小田原 伊東 静岡 名古屋 岐阜 大垣 豊橋 富山 松阪 四日市 福井 奈良 向日町 和歌山 岸和田 玉野 広島 防府 高松 小松島 高知 松山 小倉 久留米 武雄 佐世保 別府 熊本'.split(' ');
if(options.venue&&!VENUES.includes(options.venue))throw Error('会場名が不正です');
const date=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Tokyo',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
const directory=join(resolve(options['data-root']||'data'),date);
await mkdir(directory,{recursive:true});
if(options.evidence)await mkdir(resolve(options.evidence),{recursive:true});
const manifestPath=join(directory,'manifest.json');
let prior={};try{prior=JSON.parse(await readFile(manifestPath,'utf8'))}catch{}
const manifest={date,updatedAt:new Date().toISOString(),status:'running',scope:options.venue?{venue:options.venue,race:options.race||'all'}:'all',races:[],errors:[],warnings:[],completedVenues:[]};
// Partial test runs may retain previously acquired races, but never advertise
// a failed current run as complete. Full runs rebuild the index from fresh data.
if(options.venue)manifest.races=(prior.races||[]).filter(r=>r.venue!==options.venue||(options.race&&Number(r.race)!==Number(options.race)));
const saveManifest=async()=>{
  manifest.updatedAt=new Date().toISOString();
  manifest.races.sort((a,b)=>a.venue.localeCompare(b.venue,'ja')||a.race-b.race);
  await writeFile(manifestPath,JSON.stringify(manifest,null,2)+'\n');
};
const evidence=async(name,page)=>{if(options.evidence)await writeFile(join(resolve(options.evidence),name),await page.content())};
await saveManifest();

async function top(page){
  await page.goto('https://keirin.jp/pc/top',{waitUntil:'domcontentloaded',timeout:45000});
  // Generic buttons exist before the asynchronous meeting list is loaded.
  await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(b=>b.textContent.replace(/[\s　]/g,'').includes('出走表一覧')),{timeout:30000});
}
async function discover(page){
  await top(page);
  return page.evaluate(known=>[...new Set([...document.querySelectorAll('tr')].filter(row=>[...row.querySelectorAll('button')].some(b=>b.textContent.replace(/[\s　]/g,'').includes('出走表一覧'))).map(row=>known.find(v=>row.querySelector('td')?.textContent.replace(/[\s　]/g,'').startsWith(v))).filter(Boolean))],VENUES);
}
async function openRaceList(page,venue){
  await top(page);
  await page.evaluate(v=>{
    const norm=s=>s.replace(/[\s　]/g,'');
    const row=[...document.querySelectorAll('tr')].find(r=>norm(r.querySelector('td')?.textContent||'').startsWith(v)&&[...r.querySelectorAll('button')].some(b=>norm(b.textContent).includes('出走表一覧')));
    if(!row)throw Error('本日の会場なし');
    [...row.querySelectorAll('button')].find(b=>norm(b.textContent).includes('出走表一覧')).click();
  },venue);
  await page.waitForSelector('#sldivSyusouList a.sllink_name',{timeout:30000});
  const context=await page.evaluate(()=>({date:document.querySelector('#hhSelK')?.value,venueCode:document.querySelector('#hhSelJ')?.value}));
  if(context.date!==date.replaceAll('-','')||!/^\d{2}$/.test(context.venueCode||''))throw Error('公式出走表の開催日・場コードを確認できません');
  return context;
}
async function supplement(page,raceId){
  const url=`https://keirin.netkeiba.com/race/entry/?race_id=${raceId}`;
  const response=await page.goto(url,{waitUntil:'domcontentloaded',timeout:30000});
  if(!response?.ok())throw Error(`補完元 HTTP ${response?.status()}`);
  await page.waitForSelector('#RaceCard_Table_Static [id^="name_"]',{timeout:15000});
  const riders=await page.evaluate(extractSupplement,raceId);
  await evidence(`${raceId}-netkeirin.html`,page);
  return {riders,url};
}
async function profile(page,rider){
  const url=`https://keirin.jp/pc/racerprofile?snum=${rider.snum}`;
  await page.goto(url,{waitUntil:'domcontentloaded',timeout:30000});
  await page.waitForFunction(()=>[...document.querySelectorAll('td')].some(c=>c.textContent.trim()==='競走得点'),{timeout:15000});
  const data=await page.evaluate(extractProfile);
  if(data.snum!==rider.snum)throw Error('プロフィール登録番号不一致');
  return {data,url};
}

let browser,acquired=0;
try{
  browser=await puppeteer.launch({headless:true,args:['--no-sandbox'],...(process.env.PUPPETEER_EXECUTABLE_PATH?{executablePath:process.env.PUPPETEER_EXECUTABLE_PATH}:{})});
  const page=await browser.newPage(),detail=await browser.newPage();
  const venues=await discover(page);
  if(!venues.length)throw Error('開催一覧を取得できませんでした（開催なしとは断定できません）');
  if(options.venue&&!venues.includes(options.venue))throw Error(`${options.venue}は本日の開催一覧にありません`);
  manifest.discoveredVenues=venues;await saveManifest();
  for(const venue of options.venue?[options.venue]:venues){
    try{
      const context=await openRaceList(page,venue);
      await evidence(`${date}-${venue}-official.html`,page);
      let venueRaces=0;
      for(const race of options.race?[Number(options.race)]:Array.from({length:12},(_,i)=>i+1)){
        try{
          const card=await page.evaluate(extractRace,race);
          if(!card){if(options.race)throw Error('指定レースなし');continue}
          const raceId=context.date+context.venueCode+String(race).padStart(2,'0');
          let extra=null;
          try{extra=await supplement(detail,raceId)}catch(e){manifest.warnings.push({venue,race,message:`B/S補完: ${e.message}`})}
          // Do not trust a same-number rider from another roster. Match official
          // registration IDs as well, including leading zero normalization.
          const extraValid=extra&&extra.riders.length===card.riders.length&&new Set(extra.riders.map(r=>r.number)).size===card.riders.length&&card.riders.every(r=>extra.riders.some(x=>x.number===r.number&&x.snum===r.snum));
          if(extra&&!extraValid)manifest.warnings.push({venue,race,message:'補完元と公式の車番・登録番号が一致しません。補完値は不採用。'});
          const riders=[];
          for(const rider of card.riders){
            const x=extraValid?extra.riders.find(x=>x.number===rider.number):null;
            const sources={style:rider.style?'https://keirin.jp/pc/racelist':null,score:x?.score?extra.url:null,back:x?.back!=null?extra.url:null,start:x?.start!=null?extra.url:null};
            let result={...rider,style:rider.style||x?.style||null,score:x?.score??null,back:x?.back??null,start:x?.start??null,sources};
            if(!sources.style&&x?.style)sources.style=extra.url;
            if(result.score==null||result.back==null||result.style==null){
              try{
                const fallback=await profile(detail,rider);
                for(const key of ['score','style','back'])if(result[key]==null&&fallback.data[key]!=null){result[key]=fallback.data[key];sources[key]=fallback.url}
              }catch(e){manifest.warnings.push({venue,race,number:rider.number,message:`公式プロフィール: ${e.message}`})}
            }
            riders.push(result);
          }
          const missingFields=riders.flatMap(r=>['style','back','start','score'].filter(key=>r[key]==null).map(field=>({number:r.number,field})));
          const data={date,venue,race,raceId,riders,lines:card.lines,source:extraValid?'KEIRIN.JP / netkeirin':'KEIRIN.JP',lineSource:card.lines.length?'KEIRIN.JP':null,scoreType:extraValid?'出走表掲載の競走得点':'直近4ヶ月の競走得点',sourceUrls:['https://keirin.jp/pc/racelist',...(extraValid?[extra.url]:[])],missingFields,updatedAt:new Date().toISOString()};
          await writeFile(join(directory,`${venue}-${race}.json`),JSON.stringify(data,null,2)+'\n');
          manifest.races.push({venue,race,updatedAt:data.updatedAt,missingFields:missingFields.length});
          if(missingFields.length)manifest.warnings.push({venue,race,message:'一部項目が未取得。推測値は使用していません。',missingFields});
          acquired++;venueRaces++;await saveManifest();
          console.log(`${venue} ${race}R: ${riders.length}人 / 未取得 ${missingFields.length}項目`);
          console.table(riders.map(({number,name,style,back,start,score})=>({number,name,style,B:back,S:start,score})));
        }catch(e){manifest.errors.push({venue,race,message:e.message});await saveManifest()}
      }
      if(!venueRaces)throw Error('出走表を1件も取得できませんでした');
      manifest.completedVenues.push(venue);
    }catch(e){manifest.errors.push({venue,message:e.message})}
    await saveManifest();
  }
  manifest.status=!acquired?'failed':manifest.errors.length||manifest.warnings.length?'partial':'complete';
}catch(e){manifest.errors.push({message:e.message});manifest.status=acquired?'partial':'failed'}
finally{if(browser)await browser.close();await saveManifest()}
console.log(`終了: ${manifest.status} / 今回 ${acquired}レース / ${manifest.errors.length}エラー / ${manifest.warnings.length}警告`);
if(manifest.status!=='complete')process.exitCode=1;
