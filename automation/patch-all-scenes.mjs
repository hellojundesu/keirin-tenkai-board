import {readFile,writeFile} from 'node:fs/promises';

const path='race-card.html';
const marker="<script>\n(()=>{\n  'use strict';\n  const $=id=>document.getElementById(id);";
const sentinel='all-scenes-line-placement-v1';
const wrapper=`<script data-feature="${sentinel}">
(()=>{
  'use strict';
  const applyToCurrentScene=window.keirinBoardApplyLines;
  if(typeof applyToCurrentScene!=='function')return;
  window.keirinBoardApplyLines=(lines,riders)=>{
    if(!applyToCurrentScene(lines,riders))return false;
    const phase=document.getElementById('phase');
    const place=document.getElementById('place');
    const buttons=phase?Array.from(phase.querySelectorAll('[data-stage]')):[];
    if(!place||!buttons.length)return true;
    const active=buttons.find(button=>button.classList.contains('active'));
    for(const button of buttons){
      if(!button.classList.contains('active'))button.click();
      place.click();
    }
    if(active&&!active.classList.contains('active'))active.click();
    return true;
  };
})();
</script>

`;
let source=await readFile(path,'utf8');
if(!source.includes(sentinel)){
  if(!source.includes(marker))throw Error('ライン反映処理の挿入位置が見つかりません');
  source=source.replace(marker,wrapper+marker);
}
source=source.replace('並び予想をラインに反映し、表示中のシーンに配置しました。','並び予想をラインに反映し、すべてのシーンに配置しました。');
await writeFile(path,source);
