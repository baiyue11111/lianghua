import test from 'node:test';
import assert from 'node:assert/strict';
import {benchmarkCurves} from '../../display/src/research/curves.ts';
const run=(name,points)=>({name,points});
const baseline={name:'沪深300',points:[{at:'2026-09-28',close:100},{at:'2026-09-29',close:110},{at:'2026-09-30',close:121}]};
test('strategy and index use one common date and normalize independently',()=>{
 const result=benchmarkCurves([run('策略甲',[{at:'2026-09-28',equity:200},{at:'2026-09-29',equity:210},{at:'2026-09-30',equity:220}]),run('策略乙',[{at:'2026-09-29',equity:50},{at:'2026-09-30',equity:60}])],baseline);
 assert.equal(result.start,'2026-09-29');assert.equal(result.end,'2026-09-30');
 assert.deepEqual(result.series.map(s=>s.data[0][1]),[1,1,1]);
 assert.equal(result.series[2].data[1][1],1.1);assert.equal(result.series[1].data[1][1],1.2);
});
test('missing index dates are rejected without forward-fill',()=>{
 assert.throws(()=>benchmarkCurves([run('策略',[{at:'2026-09-28',equity:100},{at:'2026-09-30',equity:102}])],{...baseline,points:baseline.points.slice(0,2)}),/缺少.*2026-09-30/);
});
test('no overlapping period and zero strategy equity fail',()=>{
 assert.throws(()=>benchmarkCurves([run('策略',[{at:'2020-01-01',equity:100}])],baseline),/共同/);
 assert.throws(()=>benchmarkCurves([run('策略',[{at:'2026-09-28',equity:0}])],baseline),/权益/);
});

test('zero ending equity is a real loss, not a missing observation',()=>{
 const plot=benchmarkCurves([run('策略',[{at:'2026-09-28',equity:100},{at:'2026-09-30',equity:0}])],baseline);
 assert.equal(plot.series[0].data[1][1],0);
});
