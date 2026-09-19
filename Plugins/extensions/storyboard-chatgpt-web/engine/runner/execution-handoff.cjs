'use strict';

const {sha256Text}=require('./operation-identity.cjs');
const {compilePack}=require('./compiled-task-adapter.cjs');

const HEX64=/^[0-9a-f]{64}$/;
function loadExportedPromptPack(raw,{source_path=null,expected_source_sha256=null}={}){
  if(typeof raw!=='string'||!raw.trim())throw new Error('exported Prompt Pack JSON text is required');
  const sourceSha=sha256Text(raw);
  if(expected_source_sha256!==null){
    if(!HEX64.test(String(expected_source_sha256)))throw new Error('expected_source_sha256 invalid');
    if(sourceSha!==expected_source_sha256)throw new Error('exported Prompt Pack source SHA mismatch');
  }
  let pack;
  try{pack=JSON.parse(raw);}catch(e){throw new Error('exported Prompt Pack is not valid JSON: '+e.message);}
  const plan=compilePack(pack);
  return {
    schema_version:'storyboard-forge-execution-handoff-1.0',
    source:{path:source_path,sha256:sourceSha},
    pack_id:plan.pack_id,
    pack_revision:plan.pack_revision,
    compiler_version:plan.compiler_version,
    task_count:plan.tasks.length,
    tasks:plan.tasks.map(x=>({
      frame_id:x.frame_id,
      group_id:x.group_id,
      operation:x.operation,
      provider_required:x.provider_required,
      compiled_task_sha256:x.compiled_task_sha256
    })),
    pack,
    plan
  };
}

module.exports={loadExportedPromptPack};
