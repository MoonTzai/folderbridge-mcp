'use strict';

const Core=require('../compiler/storyboard-forge-core.js');
const {compiledTaskSha256}=require('./operation-identity.cjs');

const PROVIDER_OPERATIONS=new Set(['generate','edit']);
function clone(v){return JSON.parse(JSON.stringify(v));}
function requireValidPack(pack){
  const validation=Core.validatePack(pack);
  if(!validation.ok){
    const e=new Error('Prompt Pack validation failed: '+validation.errors.map(x=>x.code+(x.at?'@'+x.at:'')).join(', '));
    e.validation=validation;throw e;
  }
  return validation;
}
function compilePack(pack){
  const validation=requireValidPack(pack),guide=Core.compileGuideView(pack),tasks=[];
  for(const group of guide.groups){
    for(const frame of group.frames){
      const task=clone(frame.task);
      tasks.push({
        frame_id:task.frame_id,
        group_id:task.group_id,
        operation:task.operation,
        provider_required:PROVIDER_OPERATIONS.has(task.operation),
        compiled_task_sha256:compiledTaskSha256(task),
        task
      });
    }
  }
  return {
    schema_version:'storyboard-forge-runner-plan-1.0',
    pack_id:pack.pack_id,
    pack_revision:pack.revision,
    compiler_version:Core.COMPILER_VERSION,
    validation,
    tasks
  };
}
function taskFor(pack,frameId){
  requireValidPack(pack);
  const task=Core.compileTask(pack,frameId);
  return {task,compiled_task_sha256:compiledTaskSha256(task),provider_required:PROVIDER_OPERATIONS.has(task.operation)};
}
function providerRequired(task){return !!task&&PROVIDER_OPERATIONS.has(task.operation);}

module.exports={PROVIDER_OPERATIONS,compilePack,taskFor,providerRequired};
