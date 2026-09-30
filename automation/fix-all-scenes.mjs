import {readFile,writeFile} from 'node:fs/promises';

const path='race-card.html';
const oldCode=`    const buttons=phase?Array.from(phase.querySelectorAll('[data-stage]')):[];
    if(!place||!buttons.length)return true;
    const active=buttons.find(button=>button.classList.contains('active'));
    for(const button of buttons){
      if(!button.classList.contains('active'))button.click();
      place.click();
    }
    if(active&&!active.classList.contains('active'))active.click();`;
const newCode=`    const stages=phase?Array.from(phase.querySelectorAll('[data-stage]'),button=>button.dataset.stage):[];
    if(!place||!stages.length)return true;
    const activeStage=phase.querySelector('[data-stage].active')?.dataset.stage;
    for(const stage of stages){
      const button=phase.querySelector(\`[data-stage="\${stage}"]\`);
      if(button&&!button.classList.contains('active'))button.click();
      place.click();
    }
    const active=phase.querySelector(\`[data-stage="\${activeStage}"]\`);
    if(active&&!active.classList.contains('active'))active.click();`;
let source=await readFile(path,'utf8');
if(source.includes(newCode))process.exit(0);
if(!source.includes(oldCode))throw Error('修正対象の全シーン配置処理が見つかりません');
source=source.replace(oldCode,newCode);
await writeFile(path,source);
