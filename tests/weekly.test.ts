import {test} from 'node:test';
import assert from 'node:assert/strict';
import {parseWeekly,weeklyFresh} from '../src/weekly';
const now=Date.now();
const sample={version:1,status:'ok',checkedAt:new Date(now).toISOString(),attemptedAt:new Date(now).toISOString(),windows:[{id:'codex',label:'Codex',windowDurationMins:10080,usedPercent:3,remainingPercent:97,resetsAt:new Date(now+86400000).toISOString()}]};
test('weekly quota is real usage percent and expires for display',()=>{
 const value=parseWeekly(sample);assert.equal(value.windows[0].remainingPercent,97);assert.equal(weeklyFresh(value,now),true);assert.equal(weeklyFresh(value,now+1800001),false);
});
test('weekly panel rejects invented short windows and inconsistent usage',()=>{
 for(const mutate of [(v:typeof sample)=>{v.windows[0].windowDurationMins=300},(v:typeof sample)=>{v.windows[0].remainingPercent=100},(v:typeof sample)=>{v.windows[0].resetsAt='invalid'}]){
  const value=structuredClone(sample);mutate(value);assert.throws(()=>parseWeekly(value));
 }
});
