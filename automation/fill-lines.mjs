import {readFile,writeFile,rename} from 'node:fs/promises';
import {join} from 'node:path';

export function mergeMissingLines(existing,fresh,now){
  // Preserve every nonempty stored value, including manually maintained lines.
  if(existing.lines!=null&&(!Array.isArray(existing.lines)||existing.lines.length))return {reason:'preserved'};
  if(!fresh)return {reason:'unavailable'};
  const riders=existing.riders;
  if(!Array.isArray(riders)||!Array.isArray(fresh.riders)||riders.length!==fresh.riders.length)return {reason:'roster-mismatch'};
  const norm=s=>String(s||'').replace(/[\s　]/g,'');
  if(new Set(riders.map(r=>r.number)).size!==riders.length||new Set(fresh.riders.map(r=>r.number)).size!==riders.length||!riders.every(r=>fresh.riders.some(x=>x.number===r.number&&(r.snum?x.snum===r.snum:norm(x.name)===norm(r.name)))))return {reason:'roster-mismatch'};
  const lines=fresh.lines;
  if(!Array.isArray(lines)||!lines.length)return {reason:'unavailable'};
  if(lines.some(g=>!Array.isArray(g)||!g.length))return {reason:'invalid-lines'};
  const ids=lines.flat();
  if(ids.length!==riders.length||new Set(ids).size!==ids.length||!ids.every(n=>Number.isInteger(n)&&riders.some(r=>r.number===n)))return {reason:'invalid-lines'};
  return {reason:'added',data:{...existing,lines,lineSource:'KEIRIN.JP',lineUpdatedAt:now}};
}

export async function fillMissingLines({directory,date,manifest,getCard,venue,race,now=()=>new Date().toISOString()}){
  const report={date,mode:'lines-only',startedAt:now(),added:[],preserved:[],unavailable:[],errors:[]};
  if(manifest.date!==date||!Array.isArray(manifest.races))throw Error('本日のmanifestがありません。朝の取得を先に実行してください。');
  for(const entry of manifest.races){
    if(venue&&entry.venue!==venue||race&&Number(entry.race)!==Number(race))continue;
    const label={venue:entry.venue,race:Number(entry.race)};
    try{
      if(!/^[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}ー]+$/u.test(entry.venue)||!Number.isInteger(label.race)||label.race<1||label.race>12)throw Error('manifestの会場・レースが不正です');
      const path=join(directory,`${entry.venue}-${label.race}.json`);
      const saved=JSON.parse(await readFile(path,'utf8'));
      if(saved.date!==date||saved.venue!==entry.venue||Number(saved.race)!==label.race)throw Error('保存済みレースの日付・会場・レース不一致');
      if(saved.lines!=null&&(!Array.isArray(saved.lines)||saved.lines.length)){report.preserved.push(label);continue}
      const fresh=await getCard(entry.venue,label.race);
      const result=mergeMissingLines(saved,fresh,now());
      if(result.reason==='added'){
        // Atomic replacement only after successful parsing and roster validation.
        await writeFile(path+'.tmp',JSON.stringify(result.data,null,2)+'\n');
        await rename(path+'.tmp',path);report.added.push(label);
      }else if(result.reason==='unavailable')report.unavailable.push(label);
      else report.errors.push({...label,message:result.reason});
    }catch(e){report.errors.push({...label,message:e.message})}
  }
  report.finishedAt=now();report.status=report.errors.length?'partial':'complete';
  await writeFile(join(directory,'line-refresh.json'),JSON.stringify(report,null,2)+'\n');
  return report;
}
