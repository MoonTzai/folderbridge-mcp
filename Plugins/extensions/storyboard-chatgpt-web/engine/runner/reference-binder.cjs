'use strict';

const {HEX64,compiledTaskSha256,referenceFingerprint}=require('./operation-identity.cjs');

const FRAME_OK=new Set(['accepted']);
const ANCHOR_OK=new Set(['approved','accepted']);
function fail(code,message){
  const e=new Error(message);e.code=code;throw e;
}
function artifactFromResolver(resolver,ref){
  if(typeof resolver==='function')return resolver(ref);
  if(!resolver||typeof resolver!=='object')return null;
  const bucket=ref.source_type==='frame'?resolver.frames:resolver.anchors;
  return bucket&&bucket[ref.source_id]||null;
}
function validateResolved(ref,a,index,{allowUnreviewed=false}={}){
  if(!a||typeof a!=='object')fail('REFERENCE_MISSING',`Missing approved reference ${ref.source_type}:${ref.source_id}`);
  const allowed=ref.source_type==='frame'?FRAME_OK:ANCHOR_OK;
  const sequenceOnly=ref.source_type==='frame'&&allowUnreviewed===true&&a.status==='generated_unreviewed';
  if(!allowed.has(a.status)&&!sequenceOnly)fail('REFERENCE_NOT_APPROVED',`Reference ${ref.source_id} status is not approved/accepted`);
  if(a.source_type&&a.source_type!==ref.source_type)fail('REFERENCE_TYPE_MISMATCH',`Reference ${ref.source_id} source_type mismatch`);
  if(a.source_id&&a.source_id!==ref.source_id)fail('REFERENCE_ID_MISMATCH',`Reference ${ref.source_id} source_id mismatch`);
  if(typeof a.artifact_id!=='string'||!a.artifact_id)fail('REFERENCE_ARTIFACT_ID_MISSING',`Reference ${ref.source_id} artifact_id missing`);
  if(!HEX64.test(String(a.sha256||a.artifact_sha256||'')))fail('REFERENCE_SHA_INVALID',`Reference ${ref.source_id} SHA-256 invalid`);
  const sha=a.sha256||a.artifact_sha256;
  if(typeof a.path!=='string'||!a.path)fail('REFERENCE_PATH_MISSING',`Reference ${ref.source_id} local path missing`);
  if(ref.artifact_id&&ref.artifact_id!==a.artifact_id)fail('REFERENCE_ARTIFACT_ID_MISMATCH',`Reference ${ref.source_id} artifact_id differs from canonical task`);
  if(ref.artifact_sha256&&ref.artifact_sha256!==sha)fail('REFERENCE_SHA_MISMATCH',`Reference ${ref.source_id} SHA differs from canonical task`);
  return {
    index,
    source_type:ref.source_type,
    source_id:ref.source_id,
    approved_only:!sequenceOnly,
    sequence_only:sequenceOnly,
    feature_scope:Array.isArray(ref.feature_scope)?ref.feature_scope.slice():[],
    artifact_id:a.artifact_id,
    artifact_sha256:sha,
    path:a.path,
    status:a.status
  };
}
function bindRequiredReferences(task,resolver,options={}){
  if(!task||task.schema_version!=='storyboard-forge-compiled-task-1.0')fail('TASK_INVALID','compiled task required');
  const refs=Array.isArray(task.required_references)?task.required_references:[];
  const bindings=refs.map((ref,index)=>{
    if(ref.approved_only!==true)fail('REFERENCE_NOT_APPROVED_ONLY',`Task reference ${ref.source_id||index} is not approved_only`);
    return validateResolved(ref,artifactFromResolver(resolver,ref),index,options);
  });
  return {
    schema_version:'storyboard-forge-reference-binding-1.0',
    frame_id:task.frame_id,
    compiled_task_sha256:compiledTaskSha256(task),
    reference_input_fingerprint:referenceFingerprint(bindings),
    references:bindings
  };
}
function reuseArtifactFromBinding(task,receipt){
  if(!task||task.operation!=='reuse')fail('REUSE_TASK_REQUIRED','reuse task required');
  if(!receipt||receipt.frame_id!==task.frame_id)fail('REFERENCE_RECEIPT_MISMATCH','reference receipt does not belong to reuse task');
  const predecessor=(receipt.references||[]).find(x=>x.source_type==='frame');
  if(!predecessor)fail('REUSE_PREDECESSOR_MISSING','reuse requires an accepted frame reference');
  return {
    artifact_id:predecessor.artifact_id,
    artifact_sha256:predecessor.artifact_sha256,
    path:predecessor.path,
    source_frame_id:predecessor.source_id
  };
}

module.exports={bindRequiredReferences,reuseArtifactFromBinding};
