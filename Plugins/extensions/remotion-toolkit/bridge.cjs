'use strict';
// FolderBridge Remotion Toolkit v0.1.0 — declarative Node SSR bridge.
// Invoked ONLY by plugin.py after project path / SHA / option validation.
const fs=require('node:fs');
const path=require('node:path');
const crypto=require('node:crypto');
const {createRequire}=require('node:module');
const hash=(p)=>crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');
const assert=(c,m)=>{if(!c)throw new Error(m)};
const extension={h264:'mp4',h265:'mp4',vp8:'webm',vp9:'webm',av1:'mp4',prores:'mov',gif:'gif',mp3:'mp3',wav:'wav',aac:'aac','h264-ts':'ts'};
const isObj=x=>x&&typeof x==='object'&&!Array.isArray(x);
const finite=(n,min,max)=>typeof n==='number'&&Number.isFinite(n)&&n>=min&&n<=max;
function asResult(x){process.stdout.write('FBR_RESULT_JSON='+JSON.stringify(x)+'\n');}
async function input(){
  let s='';for await(const chunk of process.stdin){s+=chunk;if(s.length>1000000)throw Error('Input payload exceeds 1 MiB');}
  const parsed=JSON.parse(s);assert(isObj(parsed)&&typeof parsed.action==='string','Invalid bridge payload');
  return parsed;
}
const sText=(x)=>String(x??'').slice(0,1500);
function pkgInfo(req,name){
  let loc=null,version=null,error=null;
  try {
    loc=req.resolve(name);
    let cur=path.dirname(loc);
    for(let n=0;n<9;n++){
      const file=path.join(cur,'package.json');
      if(fs.existsSync(file)){try{const x=JSON.parse(fs.readFileSync(file,'utf8'));if(x.name===name){version=x.version;break;}}catch{}}
      const next=path.dirname(cur);if(next===cur)break;cur=next;
    }
  }catch(e){error=e.code||sText(e.message);}
  return {name,resolved:!!loc,version,error};
}
function inspect(p){
  const req=createRequire(p.runtime_package??p.package);
  const names=['remotion','@remotion/bundler','@remotion/renderer','react',...(p.required_modules||[])];
  const mods=[...new Set(names)].map(name=>pkgInfo(req,name));
  const missing=mods.filter(x=>!x.resolved);
  const skew=mods.filter(x=>(x.name==='remotion'||x.name.startsWith('@remotion/'))&&x.version&&x.version!==p.version);
  const projectRoot=p.project??path.dirname(p.package);
  const runtimeRoot=p.runtime_root??projectRoot;
  const shared=path.resolve(runtimeRoot)!==path.resolve(projectRoot);
  const depsDir=path.join(runtimeRoot,'node_modules');
  return {node:process.version,modules:mods,
    dependency_resolution:shared?'SHARED_READ_ONLY_ENGINE':'PROJECT_LOCAL',
    shared_runtime_root:shared?runtimeRoot:null,
    dependency_directory_exists:fs.existsSync(depsDir),
    ready:!missing.length&&!skew.length&&fs.existsSync(depsDir)&&fs.existsSync(p.browser),
    missing:missing.map(x=>x.name),version_skew:skew.map(x=>({name:x.name,version:x.version})),browser_present:fs.existsSync(p.browser)};
}
const selectFrames=(o,comp,single)=>{
 let a=[];
 if(Array.isArray(o.frames))a=o.frames;
 else if(Array.isArray(o.seconds))a=o.seconds.map(s=>Math.round(s*comp.fps));
 else if(single)a=[Number.isInteger(o.frame)?o.frame:0];
 const out=[...new Set(a)].sort((x,y)=>x-y);
 assert(out.length>0&&out.length<=6000,'Frame list must contain 1..6000 frames');
 for(const n of out)assert(Number.isInteger(n)&&n>=0&&n<comp.durationInFrames,'Frame outside composition');
 return out;
};
function optionsMedia(o){
 const r={codec:o.codec,overwrite:false,muted:o.muted??false,
   enforceAudioTrack:o.enforce_audio_track??false};
 const fields={audio_codec:'audioCodec',audio_bitrate:'audioBitrate',video_bitrate:'videoBitrate',
 crf:'crf',hardware_acceleration:'hardwareAcceleration',pixel_format:'pixelFormat',
 color_space:'colorSpace',sample_rate:'sampleRate',concurrency:'concurrency',
 prores_profile:'proResProfile',x264_preset:'x264Preset',gop_size:'gopSize',
 every_nth_frame:'everyNthFrame',gif_loops:'numberOfGifLoops',scale:'scale',
 jpeg_quality:'jpegQuality',max_rate:'maxRate',buffer_size:'bufferSize',image_format:'imageFormat'};
 for(const [source,dest] of Object.entries(fields))if(o[source]!==undefined)r[dest]=o[source];
 if(o.frame_ranges!==undefined){
   const ranges=o.frame_ranges;
   r.frameRange=ranges.length===1?ranges[0]:ranges;
 }
 if(o.crf!==undefined&&o.video_bitrate!==undefined)throw Error('crf and video_bitrate cannot be combined');
 if(o.crf!==undefined&&o.hardware_acceleration&&o.hardware_acceleration!=='disable')throw Error('hardwareAcceleration and CRF cannot combine');
 if(['mp3','wav','aac'].includes(o.codec))r.imageFormat='none';
 if(o.codec==='gif'&&o.audio_codec)throw Error('GIF cannot contain audio');
 if(o.codec==='prores'&&o.crf!==undefined)throw Error('ProRes does not use CRF');
 if(o.every_nth_frame!==undefined&&o.codec!=='gif')throw Error('every_nth_frame only for GIF');
 return r;
}
function artifact(p,outDir){
 const st=fs.statSync(p);
 return {name:path.basename(p),relative:path.relative(outDir,p).replaceAll('\\','/'),bytes:st.size,sha256:hash(p)};
}
function safeProgress(outDir,progress){
 const file=path.join(outDir,'progress.json');
 const tmp=path.join(outDir,'progress.tmp');
 fs.writeFileSync(tmp,JSON.stringify(progress)+'\n');fs.renameSync(tmp,file);
}
async function main(p){
 const check=inspect(p),op=p.action,o=p.options||{};
 if(op==='selftest'){
   const cases=[];
   const t=(name,f)=>{try{f();cases.push({name,pass:true})}catch(e){cases.push({name,pass:false,error:sText(e.message)})}};
   t('path output map',()=>assert(extension.prores==='mov','bad extension'));
   t('no code string from options',()=>assert(!Object.hasOwn(o,'shell'),'bad option'));
   t('Node current supports JSON',()=>assert(typeof JSON.parse==='function','bad JSON'));
   t('negative frames guarded',()=>{try{selectFrames({frames:[-1]},{fps:30,durationInFrames:90},false);throw Error('not guarded')}catch(e){if(e.message==='not guarded')throw e}});
   return {status:cases.every(c=>c.pass)?'SELFTEST_PASS':'SELFTEST_FAIL',cases};
 }
 if(op==='preflight')return {status:check.ready?'ENVIRONMENT_READY':'ENVIRONMENT_BLOCKED',...check};
 const requiresBrowser=!['media-probe','silence-scan'].includes(op);
 assert(check.missing.length===0&&check.version_skew.length===0&&(!requiresBrowser||check.browser_present),'Preflight blocked: '+JSON.stringify(check));
 const requireProj=createRequire(p.runtime_package??p.package);
 const renderer=requireProj('@remotion/renderer');
 if(op==='media-probe')return {status:'MEDIA_METADATA',metadata:await renderer.getVideoMetadata(p.media_input)};
 if(op==='silence-scan'){
   const x=await renderer.getSilentParts({src:p.media_input,
      noiseThresholdInDecibels:o.noise_db??-38,minDurationInSeconds:o.min_silence_seconds??0.7});
   return {status:'SILENCE_ANALYSIS',...x};
 }
 assert(fs.existsSync(p.entry)&&fs.existsSync(p.browser),'Source or browser missing');
 const bundler=requireProj('@remotion/bundler');
 assert(typeof bundler.bundle==='function','No @remotion/bundler.bundle');
 const bundleDir=path.join(p.output_dir,'bundle');
 const runtimeRoot=p.runtime_root??p.project;
 const sharedRuntime=path.resolve(runtimeRoot)!==path.resolve(p.project);
 const moduleRoot=path.join(runtimeRoot,'node_modules');
 // Remotion's rootDir is documented as the project containing the installed
 // Remotion packages. The source entry and public assets remain in the target
 // Vibe project; Webpack resolves NPM dependencies from the audited engine.
 // No junction/link and no modification to either project's node_modules.
 const webpackOverride=sharedRuntime?(configuration)=>{
   const prepend=(list)=>[moduleRoot,...(Array.isArray(list)?list:[]).filter(x=>x!==moduleRoot)];
   return {...configuration,
     resolve:{...configuration.resolve,modules:prepend(configuration.resolve?.modules)},
     resolveLoader:{...configuration.resolveLoader,modules:prepend(configuration.resolveLoader?.modules)}};
 }:undefined;
 let lastProgress=-1;
 const serveUrl=await bundler.bundle({
   entryPoint:p.entry,rootDir:runtimeRoot,publicDir:p.public_dir??undefined,
   outDir:bundleDir,enableCaching:false,
   ...(webpackOverride?{webpackOverride}:{}),
   onProgress:(v)=>{const pct=Math.floor(v/10)*10;if(pct>lastProgress){lastProgress=pct;safeProgress(p.output_dir,{phase:'bundle',progress:pct,utc:new Date().toISOString()})}}
 });
 if(op==='bundle')return {status:'BUNDLE_CREATED',bundle_entry:path.join(bundleDir,'index.html'),index_exists:fs.existsSync(path.join(bundleDir,'index.html'))};
 const shared={serveUrl,browserExecutable:p.browser,inputProps:o.input_props??{},
   logLevel:o.log_level??'warn',timeoutInMilliseconds:o.timeout_ms??120000};
 if(op==='compositions'){
   const comps=await renderer.getCompositions(shared);
   return {status:'COMPOSITIONS_LISTED',count:comps.length,compositions:comps.slice(0,120).map(c=>({id:c.id,width:c.width,height:c.height,fps:c.fps,durationInFrames:c.durationInFrames}))};
 }
 const comp=await renderer.selectComposition({...shared,id:o.composition_id});
 assert(comp&&comp.durationInFrames>0&&comp.fps>0,'Composition invalid');
 const about={id:comp.id,width:comp.width,height:comp.height,fps:comp.fps,durationInFrames:comp.durationInFrames};
 const imgExt={png:'png',jpeg:'jpg',webp:'webp',pdf:'pdf'};
 if(op==='render-still'||op==='render-stills'){
   const frames=selectFrames(o,comp,op==='render-still');
   assert(frames.length<=(op==='render-still'?1:120),'Requested too many stills');
   const fmt=o.image_format??'png', ext=imgExt[fmt];assert(ext,'Unsupported still image format');
   const outs=[];
   let browser=null;
   try{
     browser=await renderer.openBrowser('chrome',{browserExecutable:p.browser,logLevel:o.log_level??'warn'});
     for(const frame of frames){
       const output=path.join(p.output_dir,'still-f'+String(frame).padStart(7,'0')+'.'+ext);
       assert(!fs.existsSync(output),'Still output exists');
       await renderer.renderStill({...shared,composition:comp,puppeteerInstance:browser,frame,output,imageFormat:fmt,
         scale:o.scale??1,jpegQuality:o.jpeg_quality??undefined,overwrite:false});
       outs.push(artifact(output,p.output_dir));
       safeProgress(p.output_dir,{phase:'stills',done:outs.length,total:frames.length,last_frame:frame});
     }
   }finally{if(browser)await browser.close({silent:true});}
   return {status:'STILLS_CREATED_POSTVERIFY_PENDING',composition:about,artifacts:outs};
 }
 if(op==='render-frames'){
   assert((o.image_format??'png')!=='pdf','renderFrames supports PNG/JPEG/WebP, not PDF');
   const target=path.join(p.output_dir,'frames');fs.mkdirSync(target);
   const frameOpts={...shared,composition:comp,outputDir:target,imageFormat:o.image_format??'png',
      concurrency:o.concurrency??2,scale:o.scale??1,
      onFrameUpdate:(done,frame)=>{if(done===1||done%30===0)safeProgress(p.output_dir,{phase:'frames',done,last_frame:frame})}};
   if(o.frames||o.seconds)frameOpts.frames=selectFrames(o,comp,false);
   if(o.frame_ranges)frameOpts.frameRange=o.frame_ranges.length===1?o.frame_ranges[0]:o.frame_ranges;
   if(o.frame_sequence_pattern)frameOpts.imageSequencePattern=o.frame_sequence_pattern;
   const result=await renderer.renderFrames(frameOpts);
   const outputs=fs.readdirSync(target).filter(n=>/\.(png|jpg|jpeg|webp)$/i.test(n));
   return {status:'FRAMES_CREATED_POSTVERIFY_PENDING',composition:about,frame_count:outputs.length,
      sample:outputs.slice(0,30),result_keys:Object.keys(result||{})};
 }
 if(op==='render-video'){
   assert(!o.frames&&!o.seconds&&!Number.isInteger(o.frame),'Use frame_ranges for render-video, not still frame selectors');
   assert(!['pdf','webp'].includes(o.image_format),'renderMedia imageFormat is PNG or JPEG');
   const codec=o.codec??'h264';assert(Object.hasOwn(extension,codec),'Unsupported codec');
   const filename='render.'+extension[codec],out=path.join(p.output_dir,filename);
   assert(!fs.existsSync(out),'Output exists');
   const mapped=optionsMedia({...o,codec});
   await renderer.renderMedia({...shared,...mapped,composition:comp,outputLocation:out,
     onProgress:({progress,renderedFrames,encodedFrames,stitchStage})=>{
       if(!finite(progress,0,1))return;
       const bucket=Math.floor(progress*100);
       if(bucket%5===0&&bucket!==lastProgress){
         lastProgress=bucket;safeProgress(p.output_dir,{phase:'render-media',percent:bucket,renderedFrames,encodedFrames,stitchStage});
       }
     }});
   return {status:'MEDIA_RENDERED_POSTVERIFY_PENDING',composition:about,artifact:artifact(out,p.output_dir),codec};
 }
 throw Error('Unsupported SSR operation');
}
(async()=>{
 try{const p=await input();const result=await main(p);asResult({ok:true,result});}
 catch(e){asResult({ok:false,error:{type:e?.name||'Error',message:sText(e?.message),stack:sText(e?.stack)}});process.exitCode=1;}
})();
