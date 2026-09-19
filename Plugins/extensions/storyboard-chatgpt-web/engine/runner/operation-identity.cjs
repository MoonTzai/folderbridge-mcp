'use strict';

const crypto=require('crypto');

const HEX64=/^[0-9a-f]{64}$/;
function fail(msg){throw new Error(msg);}
function sha256Text(value){return crypto.createHash('sha256').update(String(value),'utf8').digest('hex');}
function compiledTaskSha256(task){
  if(!task||task.schema_version!=='storyboard-forge-compiled-task-1.0')fail('compiled task is required');
  return sha256Text(JSON.stringify(task));
}
function normalizeBinding(binding,index){
  if(!binding||typeof binding!=='object')fail('reference binding '+index+' is invalid');
  if(!['frame','anchor'].includes(binding.source_type))fail('reference binding '+index+' source_type invalid');
  if(typeof binding.source_id!=='string'||!binding.source_id)fail('reference binding '+index+' source_id missing');
  if(typeof binding.artifact_id!=='string'||!binding.artifact_id)fail('reference binding '+index+' artifact_id missing');
  if(!HEX64.test(String(binding.artifact_sha256||'')))fail('reference binding '+index+' artifact_sha256 invalid');
  return {
    source_type:binding.source_type,
    source_id:binding.source_id,
    artifact_id:binding.artifact_id,
    artifact_sha256:binding.artifact_sha256,
    feature_scope:Array.isArray(binding.feature_scope)?binding.feature_scope.slice():[]
  };
}
function referenceFingerprint(bindings=[]){
  if(!Array.isArray(bindings))fail('reference bindings must be an array');
  return sha256Text(JSON.stringify(bindings.map(normalizeBinding)));
}
function requireSession(sessionId){
  if(typeof sessionId!=='string'||!/^[A-Za-z0-9._-]{1,180}$/.test(sessionId))fail('invalid session_id');
}
function requireAttempt(attempt){
  if(!Number.isInteger(attempt)||attempt<1)fail('attempt must be integer >= 1');
}
function generatorOperationKey({session_id,task,attempt,bindings=[]}){
  requireSession(session_id);requireAttempt(attempt);
  if(!task||!['generate','edit'].includes(task.operation))fail('generator operation requires generate/edit compiled task');
  const payload={
    schema_version:'storyboard-forge-generator-operation-1.0',
    session_id,
    frame_id:task.frame_id,
    attempt,
    operation:task.operation,
    compiled_task_sha256:compiledTaskSha256(task),
    reference_input_fingerprint:referenceFingerprint(bindings)
  };
  return sha256Text(JSON.stringify(payload));
}
function repairOperationKey({session_id,task,attempt,bindings=[],repair_substrate_sha256,repair_prompt_sha256}){
  requireSession(session_id);requireAttempt(attempt);
  if(attempt<2)fail('repair operation attempt must be >= 2');
  if(!task||!['generate','edit'].includes(task.operation))fail('repair operation requires generate/edit compiled task');
  if(!HEX64.test(String(repair_substrate_sha256||'')))fail('repair_substrate_sha256 invalid');
  if(!HEX64.test(String(repair_prompt_sha256||'')))fail('repair_prompt_sha256 invalid');
  const payload={
    schema_version:'storyboard-forge-repair-operation-1.0',
    session_id,
    frame_id:task.frame_id,
    attempt,
    compiled_task_sha256:compiledTaskSha256(task),
    reference_input_fingerprint:referenceFingerprint(bindings),
    repair_substrate_sha256,
    repair_prompt_sha256
  };
  return sha256Text(JSON.stringify(payload));
}
function judgeOperationKey({session_id,task,attempt,bindings=[],candidate_artifact_sha256}){
  requireSession(session_id);requireAttempt(attempt);
  if(!task||!['generate','edit'].includes(task.operation))fail('judge operation requires generate/edit compiled task');
  if(!HEX64.test(String(candidate_artifact_sha256||'')))fail('candidate_artifact_sha256 invalid');
  const payload={
    schema_version:'storyboard-forge-judge-operation-1.0',
    session_id,
    frame_id:task.frame_id,
    attempt,
    compiled_task_sha256:compiledTaskSha256(task),
    reference_input_fingerprint:referenceFingerprint(bindings),
    candidate_artifact_sha256
  };
  return sha256Text(JSON.stringify(payload));
}

module.exports={HEX64,sha256Text,compiledTaskSha256,referenceFingerprint,generatorOperationKey,repairOperationKey,judgeOperationKey};
