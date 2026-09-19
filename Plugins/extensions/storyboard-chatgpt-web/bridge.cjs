'use strict';

const Handoff=require('./engine/runner/execution-handoff.cjs');
const Adapter=require('./engine/runner/compiled-task-adapter.cjs');
const Binder=require('./engine/runner/reference-binder.cjs');
const Identity=require('./engine/runner/operation-identity.cjs');
const Judge=require('./engine/runner/judge-contract.cjs');
const Repair=require('./engine/runner/repair-execution.cjs');

function fail(code,message){const e=new Error(message);e.code=code;throw e;}
function parsePack(payload){
  const h=Handoff.loadExportedPromptPack(payload.raw_pack,{
    source_path:payload.source_path||null,
    expected_source_sha256:payload.source_sha256||null
  });
  return h;
}
function taskEntry(h,frameId){
  const x=h.plan.tasks.find(t=>t.frame_id===frameId);
  if(!x)fail('FRAME_NOT_FOUND','frame_id not found in Prompt Pack: '+frameId);
  return x;
}
function validateFrozenBinding(entry,receipt){
  if(!receipt||receipt.schema_version!=='storyboard-forge-reference-binding-1.0')fail('REFERENCE_RECEIPT_MISMATCH','frozen candidate reference binding is missing or invalid');
  if(receipt.frame_id!==entry.task.frame_id)fail('REFERENCE_RECEIPT_MISMATCH','frozen candidate reference binding belongs to a different frame');
  if(receipt.compiled_task_sha256!==entry.compiled_task_sha256)fail('REFERENCE_RECEIPT_MISMATCH','frozen candidate reference binding belongs to a different compiled task');
  const refs=Array.isArray(receipt.references)?receipt.references:[];
  const required=Array.isArray(entry.task.required_references)?entry.task.required_references:[];
  if(refs.length!==required.length)fail('REFERENCE_RECEIPT_MISMATCH','frozen candidate reference count differs from compiled task');
  for(let i=0;i<required.length;i++){
    const expected=required[i],actual=refs[i];
    if(!actual||actual.source_type!==expected.source_type||actual.source_id!==expected.source_id)fail('REFERENCE_RECEIPT_MISMATCH','frozen candidate reference identity differs from compiled task');
    if(expected.approved_only===true&&actual.status==='generated_unreviewed')fail('REFERENCE_RECEIPT_MISMATCH','reviewed candidate cannot depend on generated_unreviewed reference');
  }
  const fingerprint=Identity.referenceFingerprint(refs);
  if(fingerprint!==receipt.reference_input_fingerprint)fail('REFERENCE_RECEIPT_MISMATCH','frozen candidate reference fingerprint is invalid');
  return receipt;
}
function summary(h){
  const counts={generate:0,edit:0,reuse:0,post:0};
  for(const x of h.plan.tasks)counts[x.operation]=(counts[x.operation]||0)+1;
  return {
    schema_version:h.schema_version,
    source:h.source,
    pack_id:h.pack_id,
    pack_revision:h.pack_revision,
    compiler_version:h.compiler_version,
    task_count:h.task_count,
    operation_counts:counts,
    first_frame_id:h.tasks[0]?.frame_id||null,
    tasks:h.tasks
  };
}
function handle(payload){
  const h=parsePack(payload);
  if(payload.action==='pack-inspect')return summary(h);
  const frameId=payload.frame_id;
  const entry=taskEntry(h,frameId);
  if(payload.action==='task-inspect')return entry;
  if(payload.action==='judge-validate')return Judge.computeEffectiveVerdict(payload.judge_result,payload.expected_identity||{});
  if(payload.action==='judge-preview-frozen'){
    const attempt=Number.isInteger(payload.attempt)?payload.attempt:1;
    const artifact=payload.candidate_artifact_sha256;
    const receipt=validateFrozenBinding(entry,payload.reference_binding);
    return {
      judge_operation_key:Identity.judgeOperationKey({
        session_id:payload.session_id,task:entry.task,attempt,bindings:receipt.references,candidate_artifact_sha256:artifact
      }),
      judge_envelope:Judge.buildJudgeEnvelope(entry.task,{attempt,artifact_sha256:artifact,reference_binding:receipt}),
      reference_binding:receipt
    };
  }
  if(payload.action==='repair-preview-frozen'){
    const receipt=validateFrozenBinding(entry,payload.reference_binding);
    const execution=Repair.buildRepairExecution({
      task:entry.task,
      next_attempt:payload.next_attempt,
      failed_artifact:payload.failed_artifact,
      minimum_repair_delta:payload.minimum_repair_delta,
      reference_binding:receipt
    });
    return {...execution,repair_operation_key:Identity.repairOperationKey({
      session_id:payload.session_id,
      task:entry.task,
      attempt:payload.next_attempt,
      bindings:receipt.references,
      repair_substrate_sha256:execution.substrate.artifact_sha256,
      repair_prompt_sha256:execution.repair_prompt_sha256
    })};
  }
  const receipt=Binder.bindRequiredReferences(entry.task,payload.resolver||{frames:{},anchors:{}},{allowUnreviewed:payload.allow_unreviewed===true});
  if(payload.action==='reference-bind')return receipt;
  if(payload.action==='operation-preview'){
    if(entry.task.operation==='reuse'){
      return {provider_required:false,operation:'reuse',reference_binding:receipt,reuse_artifact:Binder.reuseArtifactFromBinding(entry.task,receipt)};
    }
    if(entry.task.operation==='post')return {provider_required:false,operation:'post',reference_binding:receipt};
    const attempt=Number.isInteger(payload.attempt)?payload.attempt:1;
    return {
      provider_required:true,
      operation:entry.task.operation,
      compiled_task_sha256:entry.compiled_task_sha256,
      reference_binding:receipt,
      generator_operation_key:Identity.generatorOperationKey({
        session_id:payload.session_id,task:entry.task,attempt,bindings:receipt.references
      })
    };
  }
  if(payload.action==='judge-preview'){
    const attempt=Number.isInteger(payload.attempt)?payload.attempt:1;
    const artifact=payload.candidate_artifact_sha256;
    return {
      judge_operation_key:Identity.judgeOperationKey({
        session_id:payload.session_id,task:entry.task,attempt,bindings:receipt.references,candidate_artifact_sha256:artifact
      }),
      judge_envelope:Judge.buildJudgeEnvelope(entry.task,{attempt,artifact_sha256:artifact,reference_binding:receipt}),
      reference_binding:receipt
    };
  }
  if(payload.action==='repair-preview'){
    const execution=Repair.buildRepairExecution({
      task:entry.task,
      next_attempt:payload.next_attempt,
      failed_artifact:payload.failed_artifact,
      minimum_repair_delta:payload.minimum_repair_delta,
      reference_binding:receipt
    });
    return {...execution,repair_operation_key:Identity.repairOperationKey({
      session_id:payload.session_id,
      task:entry.task,
      attempt:payload.next_attempt,
      bindings:receipt.references,
      repair_substrate_sha256:execution.substrate.artifact_sha256,
      repair_prompt_sha256:execution.repair_prompt_sha256
    })};
  }
  fail('ACTION_NOT_FOUND','unsupported bridge action: '+payload.action);
}
async function main(){
  let raw='';for await(const chunk of process.stdin)raw+=chunk;
  try{
    const payload=JSON.parse(raw||'{}'),result=handle(payload);
    process.stdout.write(JSON.stringify({ok:true,result}));
  }catch(e){
    process.stdout.write(JSON.stringify({ok:false,error:{code:e.code||'BRIDGE_FAILED',message:String(e.message||e)}}));
    process.exitCode=1;
  }
}
main();
