import {label} from './locale.ts';

const integerFields = new Set(['shares','quantity','volume','observations','rows','count','index','pages','total']);
const priceFields = new Set(['price','open','high','low','close','preclose','pre_close','raw_close','adj_close']);
const moneyFields = new Set(['amount','gross_amount','commission','stamp_duty','transfer_fee','fee','cash','equity','initial_equity','final_equity','market_value','cost','realized_pnl','unrealized_pnl']);

function fieldPrecision(field?:string):Intl.NumberFormatOptions {
  const name=field?.toLowerCase()??'';
  if(integerFields.has(name)) return {minimumFractionDigits:0,maximumFractionDigits:0};
  if(priceFields.has(name)||moneyFields.has(name)||/(?:_price|_amount|_equity|_pnl)$/.test(name)) return {minimumFractionDigits:2,maximumFractionDigits:2};
  return {minimumFractionDigits:0,maximumFractionDigits:4};
}

function displayValue(value:unknown,field?:string):unknown {
  if(typeof value==='number') {
    if(!Number.isFinite(value)) return String(value);
    const precision=fieldPrecision(field);
    return new Intl.NumberFormat('zh-CN',precision).format(value);
  }
  if(typeof value==='string') {
    if(field && (priceFields.has(field.toLowerCase())||moneyFields.has(field.toLowerCase()))) {
      const number=Number(value);
      if(Number.isFinite(number)) return displayValue(number,field);
    }
    return label(value);
  }
  if(Array.isArray(value)) return value.map(item=>displayValue(item,field));
  if(value!==null&&typeof value==='object') {
    return Object.fromEntries(Object.entries(value).map(([key,item])=>[label(key),displayValue(item,key)]));
  }
  return value;
}

export function show(value:unknown,field?:string):string {
  if(value===null||value===undefined) return '未知';
  const displayed=displayValue(value,field);
  return typeof displayed==='object'?JSON.stringify(displayed) as string:String(displayed);
}
