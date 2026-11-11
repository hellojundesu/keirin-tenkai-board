import {rename,mkdir,writeFile,readFile,cp} from 'node:fs/promises';
import {dirname,join,resolve,sep,basename} from 'node:path';

export const VENUE_CODES=Object.fromEntries('11:函館 12:青森 13:いわき平 21:弥彦 22:前橋 23:取手 24:宇都宮 25:大宮 26:西武園 27:京王閣 28:立川 31:松戸 32:千葉 34:川崎 35:平塚 36:小田原 37:伊東 38:静岡 42:名古屋 43:岐阜 44:大垣 45:豊橋 46:富山 47:松阪 48:四日市 51:福井 53:奈良 54:向日町 55:和歌山 56:岸和田 61:玉野 62:広島 63:防府 71:高松 73:小松島 74:高知 75:松山 81:小倉 83:久留米 84:武雄 85:佐世保 86:別府 87:熊本'.split(' ').map(x=>x.split(':').reverse()));
export class AcquisitionError extends Error{
 constructor(code,message,details={}){super(message);this.code=code;this.details=details}
}
export function accessStopped(error){return error?.code==='ACCESS_RESTRICTED'||/HTTP\s*(401|403|429)|CAPTCHA|認証ページ/i.test(error?.message||'')}
export function checkOfficialDate(context,date,venue){
 if(context.date!==date.replaceAll('-',''))throw new AcquisitionError('RACECARD_DATE_MISMATCH',`日付不整合：対象日 ${date} / KEIRIN.JP掲載日 ${context.date||'確認不可'}`,{targetDate:date,actualDate:context.date??null});
 if(context.venueCode!==VENUE_CODES[venue])throw new AcquisitionError('RACECARD_IDENTITY_MISMATCH',`競輪場不一致：${venue} / 場コード ${context.venueCode||'確認不可'}`);
 return context;
}
export function validateRacecard(card,{date,venue,race,raceId,starters}){
 if(!/^\d{12}$/.test(raceId)||raceId!==date.replaceAll('-','')+VENUE_CODES[venue]+String(race).padStart(2,'0'))throw new AcquisitionError('RACECARD_IDENTITY_MISMATCH',`対象日・競輪場・レースIDが一致しません：${raceId}`);
 const riders=card?.riders;
 if(!Array.isArray(riders)||riders.length<5||riders.length>9||starters&&starters!==riders.length||new Set(riders.map(r=>r.number)).size!==riders.length||new Set(riders.map(r=>r.snum)).size!==riders.length||riders.some(r=>!Number.isInteger(r.number)||r.number<1||r.number>9||!/^\d{6}$/.test(r.snum||'')||!r.name?.trim()))throw new AcquisitionError('RACECARD_ROSTER_MISMATCH',`${venue} ${race}R：車番・登録番号・選手名・出走人数を照合できません`);
 if(card.lines?.length){const ids=card.lines.flat();if(ids.length!==riders.length||new Set(ids).size!==ids.length||ids.some(n=>!riders.some(r=>r.number===n)))throw new AcquisitionError('RACECARD_LINE_MISMATCH',`${venue} ${race}R：ラインと出走選手が一致しません`)}
 return card;
}
export async function primaryThenSecondary(primary,secondary,{date,label,onFallback=()=>{}}){
 try{return await primary()}catch(first){
  if(accessStopped(first))throw first;
  onFallback({source:'KEIRIN.JP',code:first.code??'PRIMARY_UNAVAILABLE',message:first.message});
  try{return await secondary(first)}catch(second){
   if(accessStopped(second))throw second;
   const mismatch=first.code==='RACECARD_DATE_MISMATCH';
   throw new AcquisitionError(mismatch?'RACECARD_DATE_MISMATCH':'RACECARD_UNAVAILABLE',`${date} ${label}：${mismatch?'日付不整合で出走表取得できず':'両取得元から出走表取得できず'}（KEIRIN.JP: ${first.message} / netkeirin: ${second.message}）`,{primary:{code:first.code??'PRIMARY_UNAVAILABLE',message:first.message},secondary:{code:second.code??'SECONDARY_UNAVAILABLE',message:second.message}});
  }
 }
}
export function scheduleUrl(date,code=''){return `https://keirin.netkeiba.com/race/api_race_list.html?${new URLSearchParams({pid:'api_race_list',kaisai_date:date.replaceAll('-',''),jyocd:code})}`}
export async function stageDay(publicDirectory,preserveExisting=false){
 const work=join(dirname(resolve(publicDirectory)),'..','.racecard-work',`${publicDirectory.split(/[\\/]/).at(-1)}-${Date.now()}-${process.pid}`);
 await mkdir(work,{recursive:true});
 if(preserveExisting)try{await cp(publicDirectory,work,{recursive:true,errorOnExist:false})}catch(e){if(e.code!=='ENOENT')throw e}
 return work;
}
export async function commitDay(stage,destination,manifest){
 if(manifest.status!=='complete'||manifest.errors?.length||manifest.warnings?.length)throw Error('未完成の出走表は公開先へ反映しません');
 const permitted=resolve(dirname(destination),'..','.racecard-work')+sep;
 if(!/^\d{4}-\d{2}-\d{2}$/.test(basename(destination))||!resolve(stage).startsWith(permitted))throw Error('反映先と作業フォルダの対応を確認できません');
 const backup=stage+'.previous';let prior=false;
 await mkdir(dirname(destination),{recursive:true});
 try{await rename(destination,backup);prior=true}catch(e){if(e.code!=='ENOENT')throw e}
 try{await rename(stage,destination)}catch(error){if(prior)await rename(backup,destination);throw error}
 return prior?backup:null;
}
export async function failureReport(report,filename){if(!filename)return;await mkdir(dirname(filename),{recursive:true});await writeFile(filename,JSON.stringify(report,null,2)+'\n')}
