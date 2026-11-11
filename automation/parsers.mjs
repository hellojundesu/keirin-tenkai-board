// Each exported parser is self-contained: Puppeteer serializes the function
// without its module scope when passing it to page.evaluate().
export function extractRace(race) {
  const cards=[...document.querySelectorAll('#sldivSyusouList > table.sltbl_02, #sldivSyusouList > table.sltbl_02-2')];
  const card=cards.find(t=>new RegExp(`^\\s*${race}R(?:\\s|　)`).test(t.innerText));
  if(!card)return null;
  const riders=[...card.querySelectorAll('a.sllink_name')].map(a=>{
    let cell=a.closest('td'),number=null;
    for(let i=0;i<4&&cell;i++,cell=cell.previousElementSibling){
      if(/^sltb-no\d/.test(cell.className)){number=Number(cell.textContent.trim());break}
    }
    let style=null;
    for(let next=a.closest('td').nextElementSibling;next&&!/^sltb-no\d/.test(next.className);next=next.nextElementSibling){
      const text=next.textContent.trim();
      if(/^(逃|両|追)$/.test(text)){style=text;break}
    }
    return {number,name:a.textContent.replace(/\s+/g,' ').trim(),snum:a.getAttribute('onclick')?.match(/\d{6}/)?.[0]||null,style};
  });
  if(riders.length<5||riders.length>9||riders.some(r=>!Number.isInteger(r.number)||r.number<1||r.number>9||!r.snum)||new Set(riders.map(r=>r.number)).size!==riders.length)throw Error(`${race}R: 車番または選手を確認できません`);
  const row=card.querySelector(`[id^="slyoso_td_${race}_"]`)?.closest('tr');
  const cells=row?[...row.querySelectorAll('td')].map(td=>Number(td.textContent.trim())||0):[];
  const lines=[],seen=new Set();let group=[];
  for(const n of cells){
    if(n){if(!seen.has(n)){group.push(n);seen.add(n)}}
    else if(group.length){lines.push(group);group=[]}
  }
  if(group.length)lines.push(group);
  return {race,riders,lines:seen.size===riders.length&&riders.every(r=>seen.has(r.number))?lines:[]};
}

export function extractProfile() {
  const norm=s=>s.replace(/[\s　]/g,'');
  function valueFor(label){
    // Profile headers also occur midway through a table, not just in row 1.
    for(const cell of document.querySelectorAll('td.tbl_header, th')){
      if(norm(cell.textContent)!==label)continue;
      const row=cell.closest('tr'),index=[...row.cells].indexOf(cell);
      const value=row.nextElementSibling?.cells?.[index]?.textContent.trim();
      if(value)return value;
    }
    return null;
  }
  const score=valueFor('競走得点'),style=valueFor('脚質'),back=valueFor('バック回数');
  return {snum:document.querySelector('#PlayerNo')?.textContent.trim(),
    score:/^(?:\d{2,3}\.\d{1,2}|0(?:\.0{1,2})?)$/.test(score||'')?score:null,
    style:/^(逃|両|追)$/.test(style||'')?style:null,
    back:/^\d{1,3}(?:回)?$/.test(back||'')?Number(back.replace('回','')):null};
}

export function extractSupplementLines(expectedRaceId) {
  const canonical=document.querySelector('link[rel="canonical"]')?.href;
  if(!canonical||new URL(canonical).searchParams.get('race_id')!==expectedRaceId)throw Error('ライン補完レースID不一致');
  const section=document.querySelector('section.Contents_Box.DeployYoso');
  if(!section)return [];
  const groups=[[]];
  for(const box of section.querySelectorAll('.Shaban_InBox')){
    const text=box.querySelector('.Shaban_Num')?.textContent.trim();
    if(text&&/^[1-9]$/.test(text))groups.at(-1).push(Number(text));
    else if(box.querySelector('.WakuSeparat')&&groups.at(-1).length)groups.push([]);
  }
  return groups.filter(group=>group.length);
}

export function extractSupplement(expectedRaceId) {
  const canonical=document.querySelector('link[rel="canonical"]')?.href;
  if(!canonical||new URL(canonical).searchParams.get('race_id')!==expectedRaceId)throw Error('補完出走表のレースID不一致');
  const norm=s=>s.replace(/[\s　]/g,'');
  // Ignore hidden sort keys (e.g. "1逃") without relying on column positions.
  const visibleText=cell=>{
    if(!cell)return '';
    const copy=cell.cloneNode(true);
    copy.querySelectorAll('[style*="display: none"],[style*="display:none"],script').forEach(n=>n.remove());
    return norm(copy.textContent);
  };
  const table=document.querySelector('#RaceCard_Table_Static');
  if(!table)throw Error('補完出走表のテーブルなし');
  const header=[...table.querySelectorAll('thead tr')].find(r=>[...r.cells].some(c=>norm(c.textContent)==='S'));
  if(!header)throw Error('Sヘッダーなし');
  const labels=[...header.cells].map(c=>norm(c.textContent));
  const indices=Object.fromEntries(['車','脚質','S','B','競走得点'].map(label=>[label,labels.indexOf(label)]));
  if(Object.values(indices).some(i=>i<0)||[...header.cells].some(c=>c.colSpan!==1))throw Error('補完出走表の列構造が変更されています');
  const count=text=>/^\d{1,3}$/.test(text)?Number(text):null;
  return [...table.querySelectorAll('tbody tr')].flatMap(row=>{
    const link=row.querySelector('[id^="name_"]');
    if(!link)return [];
    const match=link.id.match(/^name_(\d+)_(\d{12})$/);
    if(!match||match[2]!==expectedRaceId)throw Error('選手リンクのレースID不一致');
    if(row.cells.length!==header.cells.length)throw Error('補完出走表のセル数不一致');
    const read=label=>visibleText(row.cells[indices[label]]);
    const score=read('競走得点'),style=read('脚質');
    return [{number:count(read('車')),snum:match[1].padStart(6,'0'),name:visibleText(link.querySelector('.PlayerName')), 
      score:/^(?:\d{2,3}\.\d{1,2}|0(?:\.0{1,2})?)$/.test(score)?score:null,
      style:/^(逃|両|追)$/.test(style)?style:null,back:count(read('B')),start:count(read('S'))}];
  });
}


// Read-only parsers: no calls to authenticated or betting endpoints.
export function extractNetSchedule({date,venueCode,known}){
 const compact=date.replaceAll('-',''),norm=s=>s.replace(/[\s　]/g,'');
 const venues=[...document.querySelectorAll('li[id^="btn_jyoname_"]')].map(el=>{
  const code=el.id.match(/^btn_jyoname_(\d{2})$/)?.[1],name=known[code];
  const text=norm(el.querySelector('a')?.textContent||'');
  if(!name||!text.startsWith(name))throw Error(`競輪場コードと名称が一致しません: ${code}`);
  return {code,name};
 });
 if(!venues.length||new Set(venues.map(v=>v.code)).size!==venues.length)throw Error('対象日の開催一覧が未掲載、または構造を確認できません');
 const rows=[...document.querySelectorAll('li[id^="racing_color_"]')];
 const races=rows.map(row=>{
  const raceId=row.id.match(/^racing_color_(\d{12})$/)?.[1];
  if(!raceId||!raceId.startsWith(compact)||venueCode&&raceId.slice(8,10)!==venueCode)throw Error(`日付・場コード不一致: 対象 ${date}/${venueCode||'一覧'} 実データ ${raceId||'確認不可'}`);
  const race=Number(raceId.slice(-2)),starters=row.querySelector('.Race_Data')?.textContent.match(/(\d+)車/)?.[1];
  if(!Number.isInteger(race)||race<1||race>12)throw Error('レース番号が不正です');
  return {raceId,race,starters:starters?Number(starters):null};
 });
 if(!races.length)throw Error('対象日の出走表が未掲載、またはレース一覧を確認できません');
 if(venueCode){const nums=races.map(r=>r.race).sort((a,b)=>a-b);if(new Set(nums).size!==nums.length||nums.some((n,i)=>n!==i+1))throw Error('対象日のレース一覧に欠損・重複があります')}
 return {venues,races};
}
export function extractNetContext({raceId,venue}){
 const canonical=document.querySelector('link[rel="canonical"]')?.href;
 if(!canonical||new URL(canonical).hostname!=='keirin.netkeiba.com'||new URL(canonical).searchParams.get('race_id')!==raceId)throw Error('出走表の正規URL・raceIdが一致しません');
 const description=document.querySelector('meta[name="description"]')?.content||document.title;
 const m=description.match(/(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2})R/);
 const actual=m?m[1]+m[2].padStart(2,'0')+m[3].padStart(2,'0')+raceId.slice(8,10)+m[4].padStart(2,'0'):null;
 const tm=document.title.match(/(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2})R/);
 if(tm&&tm[1]+tm[2].padStart(2,'0')+tm[3].padStart(2,'0')+raceId.slice(8,10)+tm[4].padStart(2,'0')!==raceId)throw Error('出走表タイトルの日付・R不一致');
 const label=document.querySelector('.RaceList_NameBox h2')?.textContent.replace(/[\s　]/g,'').replace(/競輪場$/,'');
 if(actual!==raceId||!(label===venue||venue==='伊東'&&label==='伊東温泉'))throw Error(`出走表の開催日・会場・R不一致: 対象 ${raceId}/${venue} 実表示 ${actual||'確認不可'}/${label||'確認不可'}`);
 return {date:`${raceId.slice(0,4)}-${raceId.slice(4,6)}-${raceId.slice(6,8)}`,venueCode:raceId.slice(8,10),race:Number(raceId.slice(-2)),canonical};
}
export function extractOfficialRaces(){
 const tables=[...document.querySelectorAll('#sldivSyusouList > table.sltbl_02, #sldivSyusouList > table.sltbl_02-2')];
 const numbers=tables.map(t=>Number(t.innerText.match(/^\s*(\d{1,2})R(?:\s|　)/)?.[1]));
 if(!numbers.length||numbers.some(n=>!Number.isInteger(n)||n<1||n>12)||new Set(numbers).size!==numbers.length)throw Error('公式出走表のレース番号を確認できません');
 return numbers.sort((a,b)=>a-b);
}
