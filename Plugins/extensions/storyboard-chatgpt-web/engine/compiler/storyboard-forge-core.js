'use strict';

const COMPILER_VERSION = 'storyboard-forge-compiler-0.1.0';
const PACK_SCHEMA = 'storyboard-forge-prompt-pack-1.0';
const TASK_SCHEMA = 'storyboard-forge-compiled-task-1.0';

const GENERATED_MODES = new Set(['from_scratch','anchor','continuation','delta_edit','motivated_cut']);
const EDIT_MODES = new Set(['continuation','delta_edit','motivated_cut']);
const MODES = new Set([...GENERATED_MODES, 'reuse', 'post']);

function clone(v){ return JSON.parse(JSON.stringify(v)); }
function arr(v){ return Array.isArray(v) ? v : []; }
function nonEmpty(v){ return typeof v === 'string' && v.trim().length > 0; }
function uniq(values){ return [...new Set(values.filter(Boolean))]; }

function bullet(title, values){
  const xs=arr(values).filter(nonEmpty);
  if(!xs.length) return '';
  return `【${title}】\n${xs.map(x=>`- ${x}`).join('\n')}`;
}

function frameIndex(pack){
  const frames=new Map();
  const groups=new Map();
  for(const group of arr(pack.groups)){
    groups.set(group.id, group);
    for(const frame of arr(group.frames)){
      frames.set(frame.id, {group, frame});
    }
  }
  return {frames, groups};
}

function validatePack(pack){
  const errors=[], warnings=[];
  const err=(code,message,at='')=>errors.push({code,message,at});
  const warn=(code,message,at='')=>warnings.push({code,message,at});

  if(!pack || typeof pack!=='object') {
    err('PACK_OBJECT','Prompt Pack must be an object.');
    return {ok:false,errors,warnings,counts:{groups:0,frames:0,generated:0,reuse:0,post:0}};
  }
  if(pack.schema_version!==PACK_SCHEMA) err('SCHEMA_VERSION',`Expected ${PACK_SCHEMA}.`,'schema_version');
  if(!nonEmpty(pack.pack_id)) err('PACK_ID','pack_id is required.','pack_id');
  if(!nonEmpty(pack.revision)) err('REVISION','revision is required.','revision');
  if(!nonEmpty(pack.methodology_version)) err('METHODOLOGY','methodology_version is required.','methodology_version');

  const anchorIds=new Set();
  for(const [i,a] of arr(pack.anchors).entries()){
    const at=`anchors[${i}]`;
    if(!nonEmpty(a?.id)) err('ANCHOR_ID','Anchor id is required.',at);
    else if(anchorIds.has(a.id)) err('ANCHOR_DUP','Anchor id must be unique.',at);
    else anchorIds.add(a.id);
    if(!arr(a?.feature_scope).length) err('ANCHOR_SCOPE','Anchor must authorize at least one feature scope.',at);
    if(a?.artifact?.sha256 && !/^[0-9a-f]{64}$/.test(a.artifact.sha256)) err('ANCHOR_SHA','Invalid anchor artifact SHA-256.',at);
  }

  const discardedIds=new Set();
  for(const [i,d] of arr(pack.discarded_legacy).entries()){
    const at=`discarded_legacy[${i}]`;
    if(!nonEmpty(d?.id)) err('DISCARDED_ID','Discarded legacy id is required.',at);
    else if(discardedIds.has(d.id)) err('DISCARDED_DUP','Discarded legacy id must be unique.',at);
    else discardedIds.add(d.id);
  }

  const humanPolicies=pack.global?.human_policies || {};
  const groupIds=new Set(), frameIds=new Set();
  let previousFrame=null, generated=0, reuse=0, post=0, frameCount=0;

  const groups=arr(pack.groups);
  if(!groups.length) err('GROUPS','At least one group is required.','groups');

  groups.forEach((group,gi)=>{
    const gat=`groups[${gi}]`;
    if(!nonEmpty(group?.id)) err('GROUP_ID','Group id is required.',gat);
    else if(groupIds.has(group.id)) err('GROUP_DUP','Group id must be unique.',gat);
    else groupIds.add(group.id);
    if(group?.order!==gi+1) err('GROUP_ORDER',`Group order must be ${gi+1}.`,gat);
    if(!nonEmpty(group?.semantic_goal)) err('GROUP_SEMANTIC','semantic_goal is required.',gat);
    if(!nonEmpty(group?.visual_metaphor)) err('GROUP_METAPHOR','visual_metaphor is required.',gat);

    const frames=arr(group?.frames);
    if(!frames.length) err('FRAMES','Group needs at least one frame.',gat);

    frames.forEach((frame,fi)=>{
      frameCount++;
      const fat=`${gat}.frames[${fi}]`;
      if(!nonEmpty(frame?.id)) err('FRAME_ID','Frame id is required.',fat);
      else if(frameIds.has(frame.id)) err('FRAME_DUP','Frame id must be unique.',fat);
      else frameIds.add(frame.id);

      if(frame?.order!==fi+1) err('FRAME_ORDER',`Frame order must be ${fi+1} within group.`,fat);
      if(!MODES.has(frame?.generation_mode)) err('MODE',`Unknown generation_mode: ${frame?.generation_mode}`,fat);
      if(!nonEmpty(humanPolicies[frame?.human_policy])) err('HUMAN_POLICY',`Unknown human_policy: ${frame?.human_policy}`,fat);
      if(!nonEmpty(frame?.semantic?.objective)) err('OBJECTIVE','semantic.objective is required.',fat);
      if(!arr(frame?.semantic?.silent_proof).length) err('SILENT_PROOF','At least one silent_proof is required.',fat);
      if(!arr(frame?.review?.acceptance).length) err('ACCEPTANCE','At least one review acceptance rule is required.',fat);

      const mode=frame?.generation_mode;
      const pred=frame?.predecessor;
      const needsPred=['continuation','delta_edit','reuse'].includes(mode);
      if(needsPred && (!pred || !nonEmpty(pred.frame_id) || pred.approved_only!==true)){
        err('PREDECESSOR_REQUIRED',`${mode} requires an approved-only predecessor.`,fat);
      }
      if(mode==='from_scratch' && pred!==null) err('FROM_SCRATCH_PREDECESSOR','from_scratch must not have a predecessor.',fat);
      if(pred && pred.frame_id===frame.id) err('SELF_PREDECESSOR','A frame cannot precede itself.',fat);
      if(pred && pred.approved_only!==true) err('APPROVED_ONLY','Predecessor must be approved_only=true.',fat);

      if(['continuation','delta_edit'].includes(mode) && !arr(frame?.visual?.change).length){
        err('DELTA_REQUIRED',`${mode} requires visual.change.`,fat);
      }
      if(mode==='reuse'){
        reuse++;
        if(arr(frame?.visual?.change).length || arr(frame?.semantic?.semantic_delta).length){
          err('REUSE_DELTA','reuse must not introduce visual.change or semantic_delta.',fat);
        }
      } else if(mode==='post') post++;
      else if(GENERATED_MODES.has(mode)) generated++;

      let hasAnchorRef=false;
      for(const [ri,ref] of arr(frame?.reference_frames).entries()){
        const rat=`${fat}.reference_frames[${ri}]`;
        if(ref?.approved_only!==true) err('REFERENCE_APPROVED_ONLY','References must be approved_only=true.',rat);
        if(discardedIds.has(ref?.source_id)) err('DISCARDED_REFERENCE','Discarded legacy asset cannot be referenced.',rat);
        if(ref?.source_type==='anchor'){
          hasAnchorRef=true;
          if(!anchorIds.has(ref.source_id)) err('ANCHOR_REFERENCE','Unknown anchor reference.',rat);
          if(!arr(ref?.feature_scope).length) err('REFERENCE_SCOPE','Anchor reference needs a feature_scope.',rat);
        } else if(ref?.source_type==='frame'){
          if(!frameIds.has(ref.source_id)) err('FRAME_REFERENCE','Frame reference must point to an earlier frame.',rat);
        } else {
          err('REFERENCE_TYPE','Reference source_type must be frame or anchor.',rat);
        }
      }
      if(mode==='anchor' && !hasAnchorRef) err('ANCHOR_MODE_REF','anchor mode requires at least one anchor reference.',fat);

      if(pred){
        if(!frameIds.has(pred.frame_id)) err('PREDECESSOR_RESOLVE','Predecessor must point to an earlier frame.',fat);
        if(previousFrame && pred.frame_id!==previousFrame){
          warn('NON_ADJACENT_PREDECESSOR',`Predecessor ${pred.frame_id} is not the immediately previous state ${previousFrame}; confirm this is intentional.`,fat);
        }
      }
      previousFrame=frame?.id || previousFrame;
    });
  });

  return {
    ok: errors.length===0,
    errors, warnings,
    counts:{groups:groups.length,frames:frameCount,generated,reuse,post}
  };
}

function requireValid(pack){
  const result=validatePack(pack);
  if(!result.ok){
    const e=new Error(`Invalid Prompt Pack: ${result.errors.map(x=>`${x.code}@${x.at}: ${x.message}`).join(' | ')}`);
    e.validation=result;
    throw e;
  }
  return result;
}

function locate(pack, frameId){
  const hit=frameIndex(pack).frames.get(frameId);
  if(!hit) throw new Error(`Unknown frame_id: ${frameId}`);
  return hit;
}

function modeOperation(mode){
  if(mode==='reuse') return 'reuse';
  if(mode==='post') return 'post';
  if(mode==='from_scratch' || mode==='anchor') return 'generate';
  return 'edit';
}

function referenceText(frame, runtime={}){
  const refs=[];
  if(frame.predecessor){
    const bound=runtime.approved_artifacts?.[frame.predecessor.frame_id] || {};
    refs.push(`- approved predecessor frame ${frame.predecessor.frame_id}${bound.artifact_id?` (artifact ${bound.artifact_id})`:''}${bound.sha256?` [sha256 ${bound.sha256}]`:''}`);
  }
  for(const ref of arr(frame.reference_frames)){
    const bound=runtime.approved_artifacts?.[ref.source_id] || ref.artifact || {};
    const scope=arr(ref.feature_scope).join('; ') || 'declared project scope';
    refs.push(`- ${ref.source_type} ${ref.source_id}${bound.artifact_id?` (artifact ${bound.artifact_id})`:''}${bound.sha256?` [sha256 ${bound.sha256}]`:''}; inherit ONLY: ${scope}`);
  }
  return refs;
}

function compilePrompt(pack, frameId, runtime={}){
  requireValid(pack);
  const {group,frame}=locate(pack,frameId);
  const mode=frame.generation_mode;
  if(mode==='reuse' || mode==='post') return '';

  const opening = mode==='from_scratch'
    ? '从零生成当前这一张关键状态图。只建立本状态要求的世界与对象，不预先绘制后续状态。'
    : mode==='motivated_cut'
      ? '生成当前关键状态图。这是有明确叙事动机的场景/隐喻切换；只继承下面明确要求保留的项目级事实和授权 reference feature。'
      : '编辑上一张已正式确认通过的关键帧，生成当前关键状态。必须基于 approved predecessor 继续，不要重新设计整个世界。';

  const refs=referenceText(frame,runtime);
  const globalSemantic=uniq([...arr(pack.global?.semantic_negative),...arr(frame.visual?.semantic_negative)]);
  const globalVisual=uniq([...arr(pack.global?.visual_negative),...arr(frame.visual?.visual_negative)]);
  const human=pack.global?.human_policies?.[frame.human_policy] || '';

  const sections=[
    opening,
    `【当前任务】${group.id}｜${group.title}｜${frame.id}｜${frame.role}\n只输出当前这一张完整图片；不要输出联系表、多格漫画或下一状态。`,
    bullet('项目概念真值',pack.global?.concept_truth),
    bullet('项目视觉语言',pack.global?.visual_bible),
    `【当前旁白，仅供理解，不要烧成正文】\n${group.narration?.text || ''}`,
    `【本组语义目标】\n${group.semantic_goal}`,
    `【视觉解释关系】\n${group.visual_metaphor}`,
    `【当前帧语义职责】\n${frame.semantic.objective}`,
    bullet('进入本帧时已经成立的事实',frame.semantic.incoming_state),
    refs.length?`【权威 Reference / Provenance】\n${refs.join('\n')}\n如果这些已批准图片不在当前生图对话，必须先实际提供对应图片；不得用文字记忆替代图像 reference。`:'',
    bullet('保持 / Preserve',frame.visual.preserve),
    bullet('这一张只改变 / Delta',frame.visual.change),
    bullet('当前语义 Delta',frame.semantic.semantic_delta),
    bullet('镜头 Delta',frame.visual.camera_delta),
    bullet('构图 Delta',frame.visual.composition_delta),
    bullet('绝对锁定 / Lock',frame.visual.lock),
    bullet('完成本帧后应该成立',frame.semantic.outgoing_state),
    human?`【人物纪律】\n${human}`:'',
    bullet('现在绝对不能提前发生 / Forbidden Advance',frame.semantic.forbidden_advance),
    bullet('语义负面约束',globalSemantic),
    bullet('视觉负面约束',globalVisual),
    bullet('本张验收',frame.review.acceptance),
    bullet('静音证明 / Silent Proof',frame.semantic.silent_proof),
    '只完成当前关键状态。除 Delta 明确要求改变的部分外，其余已经正确建立的对象身份、关系、空间拓扑、人物身份与视觉事实保持不变；禁止擅自画下一状态。'
  ].filter(Boolean);
  return sections.join('\n\n');
}

function compileRepairPrompt(pack, frameId, repairDelta='{{MINIMUM_REPAIR_DELTA}}', runtime={}){
  requireValid(pack);
  const {frame}=locate(pack,frameId);
  if(!GENERATED_MODES.has(frame.generation_mode)) return '';
  const refs=referenceText(frame,runtime);
  const locked=uniq([...arr(frame.visual?.preserve),...arr(frame.visual?.lock)]);
  return [
    '继续编辑刚刚生成但尚未通过验收的当前图片。不要回到上一关键帧重新生成，也不要进入下一关键状态。',
    `【当前帧】${frame.id}`,
    `【只修这一点 / Minimum Repair Delta】\n${repairDelta}`,
    bullet('其他内容全部保持不变',locked),
    refs.length?`【身份 / Provenance 不得漂移】\n${refs.join('\n')}`:'',
    bullet('仍然不能提前发生',frame.semantic.forbidden_advance),
    bullet('修复后验收',frame.review.acceptance),
    '只做最小范围纠偏；不要借修复机会重新设计构图、世界、人物、拓扑、色彩或新增事件，除非 repair delta 明确要求。'
  ].filter(Boolean).join('\n\n');
}

function requiredReferences(pack, frameId, runtime={}){
  requireValid(pack);
  const {frame}=locate(pack,frameId);
  const out=[];
  const seen=new Set();
  const add=(source_type,source_id,feature_scope,artifact)=>{
    const key=`${source_type}:${source_id}`;
    if(seen.has(key)) return;
    seen.add(key);
    const bound=runtime.approved_artifacts?.[source_id] || artifact || {};
    out.push({
      source_type, source_id, approved_only:true,
      feature_scope:arr(feature_scope),
      artifact_id:bound.artifact_id || null,
      artifact_sha256:bound.sha256 || null
    });
  };
  if(frame.predecessor) add('frame',frame.predecessor.frame_id,['full approved predecessor state'],null);
  for(const ref of arr(frame.reference_frames)) add(ref.source_type,ref.source_id,ref.feature_scope,ref.artifact);
  return out;
}

function compileTask(pack, frameId, runtime={}){
  requireValid(pack);
  const {group,frame}=locate(pack,frameId);
  const operation=modeOperation(frame.generation_mode);
  return {
    schema_version:TASK_SCHEMA,
    compiler_version:COMPILER_VERSION,
    pack_id:pack.pack_id,
    pack_revision:pack.revision,
    pack_sha256:runtime.pack_sha256 || null,
    frame_id:frame.id,
    group_id:group.id,
    operation,
    prompt_text:compilePrompt(pack,frameId,runtime),
    repair_prompt_template:compileRepairPrompt(pack,frameId,'{{MINIMUM_REPAIR_DELTA}}',runtime),
    required_references:requiredReferences(pack,frameId,runtime),
    review_contract:{
      objective:frame.semantic.objective,
      acceptance:clone(frame.review.acceptance),
      silent_proof:clone(frame.semantic.silent_proof),
      forbidden_advance:clone(frame.semantic.forbidden_advance),
      semantic_negative:uniq([...arr(pack.global.semantic_negative),...arr(frame.visual.semantic_negative)]),
      visual_negative:uniq([...arr(pack.global.visual_negative),...arr(frame.visual.visual_negative)]),
      human_policy:pack.global.human_policies[frame.human_policy],
      continuity_checks:clone(frame.review.continuity_checks)
    }
  };
}

function compileGuideView(pack){
  requireValid(pack);
  return {
    schema_version:'storyboard-forge-guide-view-1.0',
    pack_id:pack.pack_id,
    revision:pack.revision,
    title:pack.project.title,
    groups:pack.groups.map(group=>({
      id:group.id,
      order:group.order,
      title:group.title,
      narration:group.narration,
      frames:group.frames.map(frame=>({
        id:frame.id,
        order:frame.order,
        role:frame.role,
        action:modeOperation(frame.generation_mode),
        generation_mode:frame.generation_mode,
        previous:frame.predecessor?.frame_id || null,
        filename:frame.output.filename,
        instruction:frame.semantic.objective,
        check:frame.review.acceptance.join('；'),
        task:compileTask(pack,frame.id)
      }))
    }))
  };
}

function compilePortableAuthoringBrief(projectInput){
  if(!projectInput || typeof projectInput!=='object') throw new Error('projectInput object is required.');
  return [
    '# Storyboard Forge｜Portable LLM Authoring Brief',
    '',
    '你要把下面的 project input 转成一个且仅一个 canonical Prompt Pack JSON。',
    `目标 schema_version 必须是：${PACK_SCHEMA}`,
    '',
    '核心方法：不要把每张图当独立海报。按“旁白 → semantic_goal → visual_metaphor → incoming_state → 当前 delta → outgoing_state → forbidden_advance → silent_proof”构造连续视觉状态机。',
    '',
    '硬规则：',
    '- 连续语义默认编辑上一张 approved frame；GENERATE_NEW / motivated cut 是例外。',
    '- predecessor/reference 必须显式，且 approved_only=true。',
    '- 多历史 provenance 必须列出真实 reference；不要假设模型凭文字记住旧图。',
    '- anchor 只继承 feature_scope；禁止继承未授权人物/地形/构图。',
    '- discarded legacy 永远不能作为 substrate/style/provenance。',
    '- 每帧拆开 preserve / change / lock；continuity 要保护对象、关系、位置、topology、camera/character identity，而不只是风格。',
    '- `forbidden_advance` 单独约束“现在还不能发生什么”。',
    '- semantic_negative 与 visual_negative 分开。',
    '- repair 使用 minimum repair delta，不重新描述/设计整个世界。',
    '- Prompt Pack 保存语义状态和 delta，不把最终 provider prompt 文本当 canonical truth。',
    '- 帧数服从语义；过程性变化优先 start/bridge/end，但不要机械固定三帧。',
    '',
    '只返回符合 schema 的 Prompt Pack JSON；不要另建 Guide 数据库，不要返回一堆最终 prompt。',
    '',
    '## PROJECT_INPUT_JSON',
    JSON.stringify(projectInput,null,2)
  ].join('\n');
}

const API={
  COMPILER_VERSION,PACK_SCHEMA,TASK_SCHEMA,GENERATED_MODES,
  validatePack,frameIndex,compilePrompt,compileRepairPrompt,
  requiredReferences,compileTask,compileGuideView,compilePortableAuthoringBrief
};
if(typeof module!=='undefined'&&module.exports) module.exports=API;
if(typeof globalThis!=='undefined') globalThis.StoryboardForgeCore=API;
