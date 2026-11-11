import puppeteer from 'puppeteer';
import {mkdir,readFile,writeFile} from 'node:fs/promises';
import {resolve,join,dirname} from 'node:path';
import {parseArgs} from 'node:util';
import {extractRace,extractProfile,extractSupplement,extractSupplementLines,extractNetSchedule,extractNetContext,extractOfficialRaces} from './parsers.mjs';
import {VENUE_CODES,AcquisitionError,accessStopped,checkOfficialDate,validateRacecard,primaryThenSecondary,scheduleUrl,stageDay,commitDay,failureReport} from './racecard-fallback.mjs';
import {fillMissingLines} from './fill-lines.mjs';
import {resolveTargetDate} from './target-date.mjs';

const {values:options}=parseArgs({options:{venue:{type:'string'},race:{type:'string'},'data-root':{type:'string'},evidence:{type:'string'},schedule:{type:'string'},'target-date':{type:'string'},'lines-only':{type:'boolean',default:false}}});
if(options.race&&(!options.venue||!/^([1-9]|1[0-2])$/.test(options.race)))throw Error('--race は --venue と1〜12を指定してください');
const VENUES='函館 青森 いわき平 弥彦 前橋 取手 宇都宮 大宮 西武園 京王閣 立川 松戸 千葉 川崎 平塚 小田原 伊東 静岡 名古屋 岐阜 大垣 豊橋 富山 松阪 四日市 福井 奈良 向日町 和歌山 岸和田 玉野 広島 防府 高松 小松島 高知 松山 小倉 久留米 武雄 佐世保 別府 熊本'.split(' ');
if(options.venue&&!VENUES.includes(options.venue))throw Error('会場名が不正です');
const currentDate=resolveTargetDate();
const date=options['target-date']||resolveTargetDate({schedule:options.schedule});
const parsedDate=new Date(date+'T00:00:00Z');
if(!/^\d{4}-\d{2}-\d{2}$/.test(date)||Number.isNaN(parsedDate.valueOf())||parsedDate.toISOString().slice(0,10)!==date)throw Error('--target-date は YYYY-MM-DD で指定してください');
const publicDirectory=join(resolve(options['data-root']||'data'),date);
let directory=publicDirectory;
if(options.evidence)await mkdir(resolve(options.evidence),{recursive:true});
const evidence=async(name,page)=>{if(options.evidence)await writeFile(join(resolve(options.evidence),name),await page.content())};
if(options['lines-only'])await mkdir(directory,{recursive:true});
let manifestPath=join(directory,'manifest.json');
let prior={};try{prior=JSON.parse(await readFile(manifestPath,'utf8'))}catch{}
if(!options['lines-only']&&options['target-date']&&date!==currentDate){
  console.error(`対象日 ${date} は現在の公開日 ${currentDate} と異なるため保留します。別日へ切り替えません。`);
  process.exit(75);
}
if(!options['lines-only']&&date!==currentDate){
  console.error(`定期full取得の対象日 ${date} は現在の公開日 ${currentDate} と異なるため保留します。別日へ切り替えません。`);
  process.exit(75);
}
if(options['lines-only']){
  if(prior.date!==date||!Array.isArray(prior.races)){
    console.error(`対象日 ${date} のmanifestがないためライン補完を保留します。`);
    process.exit(75);
  }
  let lineBrowser,linePage,currentVenue;
  try{
    const ensureLinePage=async()=>{if(!lineBrowser){lineBrowser=await puppeteer.launch({headless:true,args:['--no-sandbox'],...(process.env.PUPPETEER_EXECUTABLE_PATH?{executablePath:process.env.PUPPETEER_EXECUTABLE_PATH}:{})});linePage=await lineBrowser.newPage()}};
    const report=await fillMissingLines({directory,date,manifest:prior,venue:options.venue,race:options.race,getFallback:async saved=>{
      if(!/^\d{12}$/.test(saved.raceId||'')||saved.raceId.slice(0,8)!==date.replaceAll('-','')||Number(saved.raceId.slice(10))!==Number(saved.race))throw Error('保存済みraceId不正');
      await ensureLinePage();
      currentVenue=null;
      const extra=await supplement(linePage,saved.raceId);
      const lines=await linePage.evaluate(extractSupplementLines,saved.raceId);
      currentVenue=null;
      return {raceId:saved.raceId,riders:extra.riders,lines,lineSource:'netkeirin'};
    },getCard:async(venue,race)=>{
      if(date!==currentDate)throw Error('過去日の公式出走表は参照不可。netkeirinへフォールバック');
      if(!lineBrowser){
        lineBrowser=await puppeteer.launch({headless:true,args:['--no-sandbox'],...(process.env.PUPPETEER_EXECUTABLE_PATH?{executablePath:process.env.PUPPETEER_EXECUTABLE_PATH}:{})});
        linePage=await lineBrowser.newPage();
      }
      if(currentVenue!==venue){await openRaceList(linePage,venue);currentVenue=venue}
      return linePage.evaluate(extractRace,race);
    }});
    console.log(`ライン補完: 追加 ${report.added.length} / 既存維持 ${report.preserved.length} / 掲載なし ${report.unavailable.length} / エラー ${report.errors.length}`);
    if(report.errors.length)process.exitCode=1;
  }catch(e){console.error(e.message);process.exitCode=1}
  finally{if(lineBrowser)await lineBrowser.close()}
  process.exit(process.exitCode||0);
}
directory=await stageDay(publicDirectory,Boolean(options.venue));
manifestPath=join(directory,'manifest.json');
const reportPath=join(process.env.RUNNER_TEMP||join(dirname(publicDirectory),'..','.racecard-work'),'keirin-cloud-sync','acquisition.json');
const manifest={date,updatedAt:new Date().toISOString(),status:'running',scope:options.venue?{venue:options.venue,race:options.race||'all'}:'all',races:[],errors:[],warnings:[],sourceWarnings:[],expectedRaces:[],completedVenues:[]};
// Partial test runs may retain previously acquired races, but never advertise
// a failed current run as complete. Full runs rebuild the index from fresh data.
if(options.venue)manifest.races=(prior.races||[]).filter(r=>r.venue!==options.venue||(options.race&&Number(r.race)!==Number(options.race)));
const saveManifest=async()=>{
  manifest.updatedAt=new Date().toISOString();
  manifest.races.sort((a,b)=>a.venue.localeCompare(b.venue,'ja')||a.race-b.race);
  await writeFile(manifestPath,JSON.stringify(manifest,null,2)+'\n');
};
await saveManifest();

async function top(page){
  const response=await page.goto('https://keirin.jp/pc/top',{waitUntil:'domcontentloaded',timeout:45000});
  await checkPage(page,response,'KEIRIN.JP');
  // Generic buttons exist before the asynchronous meeting list is loaded.
  await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(b=>b.textContent.replace(/[\s　]/g,'').includes('出走表一覧')),{timeout:30000});
}
async function discover(page){
  await top(page);
  const dates=await page.evaluate(()=>[...new Set((globalThis.pc0101_json?.RaceList||[]).map(r=>r.kaisaiDate))]);
  if(!dates.length||dates.some(d=>d!==date.replaceAll('-','')))throw new AcquisitionError('RACECARD_DATE_MISMATCH',`日付不整合：対象日 ${date} / KEIRIN.JP開催一覧 ${dates.join(',')||'確認不可'}`,{targetDate:date,actualDates:dates});
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
  return checkOfficialDate(context,date,venue);
}
async function supplement(page,raceId){
  const url=`https://keirin.netkeiba.com/race/entry/?race_id=${raceId}`;
  const response=await page.goto(url,{waitUntil:'domcontentloaded',timeout:30000});
  await checkPage(page,response,'netkeirin補完');
  await page.waitForSelector('#RaceCard_Table_Static [id^="name_"]',{timeout:15000});
  const riders=await page.evaluate(extractSupplement,raceId);
  await evidence(`${raceId}-netkeirin.html`,page);
  return {riders,url};
}
async function profile(page,rider){
  const url=`https://keirin.jp/pc/racerprofile?snum=${rider.snum}`;
  const response=await page.goto(url,{waitUntil:'domcontentloaded',timeout:30000});
  await checkPage(page,response,'KEIRIN.JPプロフィール');
  await page.waitForFunction(()=>[...document.querySelectorAll('td')].some(c=>c.textContent.trim()==='競走得点'),{timeout:15000});
  const data=await page.evaluate(extractProfile);
  if(data.snum!==rider.snum)throw Error('プロフィール登録番号不一致');
  return {data,url};
}

async function checkPage(page,response,label){
 const status=response?.status();
 if([401,403,429].includes(status))throw new AcquisitionError('ACCESS_RESTRICTED',`${label} HTTP ${status}：アクセス制限のため停止`);
 const blocked=await page.evaluate(()=>/captcha|access denied|just a moment/i.test(document.title)||!!document.querySelector('#challenge-form,iframe[src*="recaptcha"],iframe[src*="hcaptcha"]')||/\/login(?:\/|$)/i.test(location.pathname));
 if(blocked)throw new AcquisitionError('ACCESS_RESTRICTED',`${label} CAPTCHAまたは認証ページ：取得を停止`);
 if(!response?.ok())throw new AcquisitionError(status===404?'RACECARD_NOT_PUBLISHED':'SOURCE_HTTP_ERROR',`${label} HTTP ${status??'確認不可'}${status===404?'（未掲載）':''}`);
}
let browser,netPage,acquired=0,lastNetRequest=0;
const netSchedules=new Map(),netCards=new Map();let netIndex;
const fallbackLog=(label)=>(failure)=>{
 manifest.sourceWarnings.push({label,...failure});
 console.log(`取得元切替: ${date} ${label} / ${failure.message} → netkeirin`);
};
async function netOpen(url){
 if(!netPage){
  netPage=await browser.newPage();await netPage.setJavaScriptEnabled(false);await netPage.setRequestInterception(true);
  netPage.on('request',request=>{const u=new URL(request.url());if(request.resourceType()==='document'&&u.protocol==='https:'&&u.hostname==='keirin.netkeiba.com')request.continue();else request.abort()});
 }
 const delay=1000-(Date.now()-lastNetRequest);if(delay>0)await new Promise(r=>setTimeout(r,delay));lastNetRequest=Date.now();
 const response=await netPage.goto(url,{waitUntil:'domcontentloaded',timeout:30000});await checkPage(netPage,response,'netkeirin');
}
async function netCatalog(){
 if(!netIndex){await netOpen(scheduleUrl(date));netIndex=await netPage.evaluate(extractNetSchedule,{date,known:Object.fromEntries(Object.entries(VENUE_CODES).map(([n,c])=>[c,n]))});await evidence(`${date}-netkeirin-index.html`,netPage)}
 return netIndex;
}
async function netSchedule(venue){
 if(!netSchedules.has(venue)){
  const code=VENUE_CODES[venue],index=await netCatalog();if(!index.venues.some(v=>v.code===code&&v.name===venue))throw new AcquisitionError('RACECARD_NOT_PUBLISHED',`${date} ${venue}の出走表は開催一覧にありません`);
  await netOpen(scheduleUrl(date,code));const info=await netPage.evaluate(extractNetSchedule,{date,venueCode:code,known:Object.fromEntries(Object.entries(VENUE_CODES).map(([n,c])=>[c,n]))});
  netSchedules.set(venue,info.races);await evidence(`${date}-${venue}-netkeirin-schedule.html`,netPage);
 }
 return netSchedules.get(venue);
}
async function netRace(venue,race){
 const schedule=await netSchedule(venue),expected=schedule.find(r=>r.race===race);
 if(!expected)throw new AcquisitionError('RACECARD_NOT_PUBLISHED',`${date} ${venue} ${race}Rの出走表は未掲載です`);
 const raceId=expected.raceId;
 if(!netCards.has(raceId)){
  const url=`https://keirin.netkeiba.com/race/entry/?race_id=${raceId}`;
  await netOpen(url);await netPage.evaluate(extractNetContext,{raceId,venue});
  const riders=await netPage.evaluate(extractSupplement,raceId),lines=await netPage.evaluate(extractSupplementLines,raceId);
  validateRacecard({riders,lines},{date,venue,race,raceId,starters:expected.starters});
  const missingFields=riders.flatMap(r=>['style','back','start','score'].filter(key=>r[key]==null).map(field=>({number:r.number,field})));
  const updatedAt=new Date().toISOString();
  netCards.set(raceId,{date,venue,race,raceId,riders:riders.map(r=>({...r,sources:{style:r.style?url:null,score:r.score!=null?url:null,back:r.back!=null?url:null,start:r.start!=null?url:null}})),lines,source:'netkeirin',lineSource:lines.length?'netkeirin':null,...(lines.length?{lineUpdatedAt:updatedAt}:{}),scoreType:'出走表掲載の競走得点',sourceUrls:[url],missingFields,updatedAt});
  await evidence(`${raceId}-netkeirin.html`,netPage);
 }
 return netCards.get(raceId);
}
try{
 browser=await puppeteer.launch({headless:true,args:['--no-sandbox'],...(process.env.PUPPETEER_EXECUTABLE_PATH?{executablePath:process.env.PUPPETEER_EXECUTABLE_PATH}:{})});
 const page=await browser.newPage(),detail=await browser.newPage();
 let primaryDiscovery=true;
 const venues=await primaryThenSecondary(async()=>{const found=await discover(page);if(!found.length)throw Error('開催一覧を取得できませんでした（開催なしとは断定できません）');return found},async()=>{primaryDiscovery=false;return(await netCatalog()).venues.map(v=>v.name)},{date,label:'開催一覧',onFallback:fallbackLog('開催一覧')});
 if(options.venue&&!venues.includes(options.venue))throw Error(`${options.venue}は対象日の開催一覧にありません`);
 manifest.discoveredVenues=venues;await saveManifest();
 for(const venue of options.venue?[options.venue]:venues){
  try{
   let context,primaryVenue=true,officialFailure;
   const numbers=await primaryThenSecondary(async()=>{
    if(!primaryDiscovery)throw Error('対象日の公式開催一覧を確認できないため基本出走表を第二取得元へ切替');
    context=await openRaceList(page,venue);await evidence(`${date}-${venue}-official.html`,page);
    return await page.evaluate(extractOfficialRaces);
   },async first=>{officialFailure=first;primaryVenue=false;return(await netSchedule(venue)).map(r=>r.race)},{date,label:venue,onFallback:fallbackLog(venue)});
   manifest.expectedRaces.push(...numbers.map(race=>({venue,race,source:primaryVenue?'KEIRIN.JP':'netkeirin'})));
   let venueRaces=0;
   for(const race of options.race?[Number(options.race)]:numbers){
    try{
     const data=await primaryThenSecondary(async()=>{
      if(!primaryVenue)throw officialFailure;
          const card=await page.evaluate(extractRace,race);
          if(!card)throw Error('公式の期待レースが出走表にありません');
          const raceId=context.date+context.venueCode+String(race).padStart(2,'0');
          validateRacecard(card,{date,venue,race,raceId});
          let extra=null;
          try{extra=await supplement(detail,raceId)}catch(e){if(accessStopped(e))throw e;manifest.warnings.push({venue,race,message:`B/S補完: ${e.message}`})}
          let fallbackLines=[];
          if(!card.lines.length&&extra){
            try{fallbackLines=await detail.evaluate(extractSupplementLines,raceId)}catch(e){if(accessStopped(e))throw e;manifest.warnings.push({venue,race,message:`ライン補完: ${e.message}`})}
          }
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
              }catch(e){if(accessStopped(e))throw e;manifest.warnings.push({venue,race,number:rider.number,message:`公式プロフィール: ${e.message}`})}
            }
            riders.push(result);
          }
          if(!card.lines.length&&extraValid&&fallbackLines.length){
            const ids=fallbackLines.flat();
            if(ids.length===riders.length&&new Set(ids).size===ids.length&&ids.every(n=>riders.some(r=>r.number===n))){card.lines=fallbackLines;card.lineSource='netkeirin'}
          }
          const missingFields=riders.flatMap(r=>['style','back','start','score'].filter(key=>r[key]==null).map(field=>({number:r.number,field})));
          const data={date,venue,race,raceId,riders,lines:card.lines,source:extraValid?'KEIRIN.JP / netkeirin':'KEIRIN.JP',lineSource:card.lines.length?'KEIRIN.JP':null,scoreType:extraValid?'出走表掲載の競走得点':'直近4ヶ月の競走得点',sourceUrls:['https://keirin.jp/pc/racelist',...(extraValid?[extra.url]:[])],missingFields,updatedAt:new Date().toISOString()};
          if(card.lineSource){data.lineSource=card.lineSource;data.lineUpdatedAt=data.updatedAt}

      return data;
     },()=>netRace(venue,race),{date,label:`${venue} ${race}R`,onFallback:primaryVenue?fallbackLog(`${venue} ${race}R`):()=>{}});
     try{
      const old=JSON.parse(await readFile(join(publicDirectory,`${venue}-${race}.json`),'utf8'));
      if(old.raceId===data.raceId&&Array.isArray(old.lines)&&old.lines.length&&old.riders?.length===data.riders.length&&data.riders.every(r=>old.riders.some(x=>x.number===r.number&&x.snum===r.snum))){data.lines=old.lines;data.lineSource=old.lineSource;data.lineUpdatedAt=old.lineUpdatedAt}
     }catch(error){if(error.code!=='ENOENT')manifest.warnings.push({venue,race,message:`既存ライン確認: ${error.message}`})}
     await writeFile(join(directory,`${venue}-${race}.json`),JSON.stringify(data,null,2)+'\n');
     manifest.races.push({venue,race,updatedAt:data.updatedAt,missingFields:data.missingFields.length});
     if(data.missingFields.length)manifest.warnings.push({venue,race,message:'一部項目が未取得。推測値は使用していません。',missingFields:data.missingFields});
     acquired++;venueRaces++;await saveManifest();console.log(`${venue} ${race}R: ${data.riders.length}人 / 取得元 ${data.source} / 未取得 ${data.missingFields.length}項目`);
    }catch(e){if(accessStopped(e))throw e;manifest.errors.push({venue,race,code:e.code||'RACECARD_ACQUISITION_FAILED',message:e.message,details:e.details});console.error(`::error title=出走表取得できず::${e.message}`);await saveManifest()}
   }
   if(!venueRaces)throw Error('出走表を1件も取得できませんでした');
   manifest.completedVenues.push(venue);
  }catch(e){if(accessStopped(e))throw e;manifest.errors.push({venue,code:e.code||'RACECARD_ACQUISITION_FAILED',message:e.message,details:e.details});console.error(`::error title=出走表取得できず::${e.message}`)}
  await saveManifest();
 }
 manifest.status=!acquired?'failed':manifest.errors.length||manifest.warnings.length?'partial':'complete';
}catch(e){manifest.errors.push({code:e.code||'RACECARD_ACQUISITION_FAILED',message:e.message,details:e.details});console.error(`::error title=出走表取得できず::${e.message}`);manifest.status=acquired?'partial':'failed'}
finally{
 if(browser)await browser.close();await saveManifest();
 await failureReport({date,status:manifest.status,acquired,errors:manifest.errors,warnings:manifest.warnings,sourceWarnings:manifest.sourceWarnings},reportPath);
}
if(manifest.status==='complete'){
 try{const backup=await commitDay(directory,publicDirectory,manifest);console.log(`完全な出走表を反映: ${publicDirectory}${backup?` / 前世代保全: ${backup}`:''}`)}catch(e){manifest.status='failed';manifest.errors.push({code:'RACECARD_SAVE_FAILED',message:`対象日 ${date} の出走表反映に失敗：${e.message}`});console.error(`::error title=出走表保存失敗::対象日 ${date} / ${e.message}`);await failureReport({date,status:manifest.status,acquired,errors:manifest.errors,warnings:manifest.warnings,sourceWarnings:manifest.sourceWarnings},reportPath)}
}else console.error(`出走表取得できず: 対象日 ${date} / 正常な保存済みデータは変更していません。詳細: ${reportPath}`);
console.log(`終了: ${manifest.status} / 今回 ${acquired}レース / ${manifest.errors.length}エラー / ${manifest.warnings.length}警告`);
if(manifest.status!=='complete')process.exitCode=1;
