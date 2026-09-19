'use strict';

const {HEX64,sha256Text,compiledTaskSha256}=require('./operation-identity.cjs');

function fail(code,message){const e=new Error(message);e.code=code;throw e;}
function buildRepairExecution({task,next_attempt,failed_artifact,minimum_repair_delta,reference_binding}){
  if(!task||!['generate','edit'].includes(task.operation))fail('TASK_INVALID','repair requires generate/edit compiled task');
  if(!Number.isInteger(next_attempt)||next_attempt<2)fail('ATTEMPT_INVALID','next_attempt must be integer >= 2');
  if(!failed_artifact||failed_artifact.frame_id!==task.frame_id)fail('SUBSTRATE_FRAME_MISMATCH','failed substrate frame_id mismatch');
  if(failed_artifact.attempt!==next_attempt-1)fail('SUBSTRATE_ATTEMPT_MISMATCH','repair must edit the immediately previous failed attempt');
  const sha=failed_artifact.sha256||failed_artifact.artifact_sha256;
  if(!HEX64.test(String(sha||'')))fail('SUBSTRATE_SHA_INVALID','failed substrate SHA invalid');
  if(typeof failed_artifact.artifact_id!=='string'||!failed_artifact.artifact_id)fail('SUBSTRATE_ID_MISSING','failed substrate artifact_id missing');
  if(typeof failed_artifact.path!=='string'||!failed_artifact.path)fail('SUBSTRATE_PATH_MISSING','failed substrate path missing');
  if(failed_artifact.status==='accepted')fail('SUBSTRATE_ALREADY_ACCEPTED','accepted artifact cannot be used as a failed-attempt repair substrate');
  const delta=String(minimum_repair_delta||'').trim();
  if(!delta)fail('REPAIR_DELTA_MISSING','minimum repair delta required');
  const token='{{MINIMUM_REPAIR_DELTA}}';
  if(!String(task.repair_prompt_template||'').includes(token))fail('REPAIR_TEMPLATE_INVALID','compiled task repair template is missing delta token');
  const prompt=task.repair_prompt_template.split(token).join(delta);
  return {
    schema_version:'storyboard-forge-repair-execution-1.0',
    frame_id:task.frame_id,
    attempt:next_attempt,
    source_attempt:failed_artifact.attempt,
    compiled_task_sha256:compiledTaskSha256(task),
    minimum_repair_delta:delta,
    repair_prompt_text:prompt,
    repair_prompt_sha256:sha256Text(prompt),
    substrate:{
      artifact_id:failed_artifact.artifact_id,
      artifact_sha256:sha,
      path:failed_artifact.path
    },
    canonical_reference_binding:reference_binding||null
  };
}

module.exports={buildRepairExecution};
