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
  }).filter(x=>x.number>=1&&x.number<=9&&x.snum);
  if(riders.length<5||riders.length>9||new Set(riders.map(r=>r.number)).size!==riders.length)throw Error(`${race}R: 車番または選手を確認できません`);
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
    return [{number:count(read('車')),snum:match[1].padStart(6,'0'),
      score:/^(?:\d{2,3}\.\d{1,2}|0(?:\.0{1,2})?)$/.test(score)?score:null,
      style:/^(逃|両|追)$/.test(style)?style:null,back:count(read('B')),start:count(read('S'))}];
  });
}
