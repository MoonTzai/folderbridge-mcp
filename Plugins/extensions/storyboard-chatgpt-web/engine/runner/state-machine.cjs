'use strict';

const TRANSITIONS={
  READY:['RUNNING','STOPPED'],
  RUNNING:['DECISION_BOUNDARY','WAITING_PROVIDER','PAUSED_NEEDS_HUMAN','COMPLETED','STOPPED'],
  DECISION_BOUNDARY:['RUNNING','PAUSED_NEEDS_HUMAN','COMPLETED','STOPPED'],
  WAITING_PROVIDER:['RUNNING','PAUSED_NEEDS_HUMAN','STOPPED'],
  PAUSED_NEEDS_HUMAN:['RUNNING','STOPPED'],
  COMPLETED:[],
  STOPPED:[]
};
function transition(run,next){
  const allowed=TRANSITIONS[run.status]||[];
  if(!allowed.includes(next))throw new Error(`illegal transition ${run.status} -> ${next}`);
  const out={...run,status:next};
  if(next==='DECISION_BOUNDARY')out.mode_effective=out.mode_requested;
  return out;
}
function requestMode(run,mode){
  if(!['AUTO','HUMAN_CONFIRM'].includes(mode))throw new Error('invalid requested mode');
  const out={...run,mode_requested:mode};
  if(run.status==='DECISION_BOUNDARY')out.mode_effective=mode;
  return out;
}
function applyDecisionBoundary(run){
  if(run.status!=='DECISION_BOUNDARY')throw new Error('not at DECISION_BOUNDARY');
  return {...run,mode_effective:run.mode_requested};
}
module.exports={TRANSITIONS,transition,requestMode,applyDecisionBoundary};
