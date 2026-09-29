import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {resolve,extname,sep} from 'node:path';
const root=resolve(import.meta.dirname,'..');
const server=createServer(async(req,res)=>{
  try{
    const url=new URL(req.url,'http://127.0.0.1');
    let requestPath=decodeURIComponent(url.pathname==='/'?'/race-card.html':url.pathname);
    if(process.argv.includes('--snapshot')&&requestPath.startsWith('/data/'))requestPath='/verification'+requestPath;
    const path=resolve(root,'.'+requestPath);
    if(!path.startsWith(root+sep))throw Error('invalid path');
    const data=await readFile(path);
    res.writeHead(200,{'Content-Type':({'.html':'text/html; charset=utf-8','.json':'application/json; charset=utf-8'})[extname(path)]||'application/octet-stream','Cache-Control':'no-store'});res.end(data);
  }catch{res.writeHead(404);res.end('Not found')}
});
server.listen(Number(process.env.PORT||8765),'127.0.0.1',()=>console.log('プレビュー: http://127.0.0.1:'+server.address().port+'/race-card.html'));
