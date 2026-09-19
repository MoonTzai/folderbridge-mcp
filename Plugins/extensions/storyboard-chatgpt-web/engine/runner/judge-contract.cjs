'use strict';

const {HEX64,compiledTaskSha256}=require('./operation-identity.cjs');

const AXES=['objective','acceptance','silent_proof','forbidden_advance','semantic_negative','visual_negative','continuity','human_policy'];
const VALUES=['pass','fail','uncertain'];
function fail(message){throw new Error(message);}
function bullet(title,items){
  const xs=Array.isArray(items)?items.filter(Boolean):[];
  return `【${title}】\n${xs.length?xs.map(x=>'- '+x).join('\n'):'- 无额外条目'}`;
}
function buildJudgeEnvelope(task,{attempt,artifact_sha256,reference_binding}={}){
  if(!task||!['generate','edit'].includes(task.operation))fail('Judge requires a generate/edit compiled task');
  if(!Number.isInteger(attempt)||attempt<1)fail('attempt must be integer >= 1');
  if(!HEX64.test(String(artifact_sha256||'')))fail('artifact_sha256 invalid');
  const taskSha=compiledTaskSha256(task);
  const refFp=reference_binding?.reference_input_fingerprint||'0'.repeat(64);
  if(!HEX64.test(refFp))fail('reference binding fingerprint invalid');
  const c=task.review_contract||{};
  const refs=(reference_binding?.references||[]).map(r=>`${r.source_type}:${r.source_id} -> ${r.artifact_id} [sha256 ${r.artifact_sha256}] scope=${(r.feature_scope||[]).join(' / ')||'full'}`);
  const shape={
    schema_version:'storyboard-forge-judge-1.0',
    frame_id:task.frame_id,
    attempt,
    artifact_sha256,
    compiled_task_sha256:taskSha,
    reference_input_fingerprint:refFp,
    model_verdict:'PASS|FAIL|REVIEW',
    checks:Object.fromEntries(AXES.map(x=>[x,'pass|fail|uncertain'])),
    repair_delta:'string|null'
  };
  return [
    '你是 Storyboard Forge 的独立 Judge。只审查当前候选图片；不要生成、编辑或修复图片。',
    `【身份】\nframe_id=${task.frame_id}\nattempt=${attempt}\nartifact_sha256=${artifact_sha256}\ncompiled_task_sha256=${taskSha}\nreference_input_fingerprint=${refFp}`,
    `【当前任务目标 / objective】\n${c.objective||''}`,
    bullet('验收 acceptance',c.acceptance),
    bullet('静音证明 silent_proof',c.silent_proof),
    bullet('禁止提前发生 forbidden_advance',c.forbidden_advance),
    bullet('语义负面约束 semantic_negative',c.semantic_negative),
    bullet('视觉负面约束 visual_negative',c.visual_negative),
    bullet('连续性 continuity',c.continuity_checks),
    `【人物纪律 human_policy】\n${c.human_policy||''}`,
    `【已绑定 Reference 身份】\n${refs.length?refs.map(x=>'- '+x).join('\n'):'- 当前任务无 reference'}`,
    '判断规则：任何必需轴明确失败用 fail；看不清或无法确定用 uncertain；不要猜测。repair_delta 只写当前图片需要修正的最小差异，不重写整个分镜。',
    '只返回一个 JSON 对象，不要 Markdown，不要额外解释。必须严格使用以下结构：',
    JSON.stringify(shape,null,2)
  ].join('\n\n');
}
function validateJudgeResult(v){
  if(!v||typeof v!=='object'||Array.isArray(v))fail('judge result must be object');
  if(v.schema_version!=='storyboard-forge-judge-1.0')fail('judge schema_version mismatch');
  if(typeof v.frame_id!=='string'||!v.frame_id)fail('judge frame_id missing');
  if(!Number.isInteger(v.attempt)||v.attempt<1)fail('judge attempt invalid');
  for(const k of ['artifact_sha256','compiled_task_sha256','reference_input_fingerprint'])if(!HEX64.test(String(v[k]||'')))fail('judge '+k+' invalid');
  if(!['PASS','FAIL','REVIEW'].includes(v.model_verdict))fail('judge model_verdict invalid');
  if(!v.checks||typeof v.checks!=='object'||Array.isArray(v.checks))fail('judge checks missing');
  for(const axis of AXES)if(!VALUES.includes(v.checks[axis]))fail('judge checks.'+axis+' invalid');
  if(!(v.repair_delta===null||typeof v.repair_delta==='string'))fail('judge repair_delta must be string|null');
  return v;
}
function computeEffectiveVerdict(v,expected={}){
  try{validateJudgeResult(v);}catch(e){return {effective_verdict:'REVIEW',reasons:[e.message]};}
  const identity=[
    ['frame_id',expected.frame_id],
    ['attempt',expected.attempt],
    ['artifact_sha256',expected.artifact_sha256],
    ['compiled_task_sha256',expected.compiled_task_sha256],
    ['reference_input_fingerprint',expected.reference_input_fingerprint]
  ];
  const mismatch=identity.filter(([,wanted])=>wanted!==undefined).filter(([k,wanted])=>v[k]!==wanted).map(([k])=>k+' mismatch');
  if(mismatch.length)return {effective_verdict:'REVIEW',reasons:mismatch};
  const failed=AXES.filter(x=>v.checks[x]==='fail');
  if(failed.length)return {effective_verdict:'FAIL',reasons:failed.map(x=>'required axis failed: '+x)};
  const uncertain=AXES.filter(x=>v.checks[x]==='uncertain');
  if(uncertain.length)return {effective_verdict:'REVIEW',reasons:uncertain.map(x=>'required axis uncertain: '+x)};
  if(v.model_verdict!=='PASS')return {effective_verdict:'REVIEW',reasons:['model_verdict disagrees with all-pass required checks']};
  return {effective_verdict:'PASS',reasons:[]};
}

module.exports={AXES,buildJudgeEnvelope,validateJudgeResult,computeEffectiveVerdict};
