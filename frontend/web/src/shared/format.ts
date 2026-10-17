export const missing='未提供';
export function record(value:unknown):Record<string,unknown>{return value!==null&&typeof value==='object'&&!Array.isArray(value)?value as Record<string,unknown>:{};}
// Only business text reaches the view. Technical data stays in the source objects.
export function text(value:unknown):string|undefined{
 if(typeof value!=='string'||!value.trim())return undefined;
 const s=value.trim();
 if(/[a-f\d]{16,}|(?:[A-Za-z_]\w*\.){2,}|\b(?:plugins|frontend|src|core)\.|[\\/]|[{}\[\]]|\b(?:sha256|traceback|order_id|run_id)\b|\d{2}:\d{2}:\d{2}/i.test(s))return undefined;
 if(/^[A-Za-z\d]+(?:[_-][A-Za-z\d]+)+$/.test(s))return undefined;
 return s;
}
export function day(value:unknown):string{
 if(typeof value!=='string')return missing;
 const match=/^(\d{4})-(\d{2})-(\d{2})(?:$|T| )/.exec(value);
 if(!match)return missing;
 const candidate=match[0].slice(0,10),d=new Date(candidate+'T00:00:00Z');
 return !Number.isNaN(d.valueOf())&&d.toISOString().slice(0,10)===candidate?candidate:missing;
}
export function number(value:unknown,digits=2):string{
 if(typeof value==='number')return Number.isFinite(value)?new Intl.NumberFormat('en-US',{maximumFractionDigits:digits}).format(value):missing;
 if(typeof value!=='string')return missing;
 const parts=/^(-?)(\d+)(?:\.(\d+))?$/.exec(value.trim());
 if(!parts)return missing;
 const fraction=parts[3]??'',scale=10n**BigInt(digits);
 let units=BigInt(parts[2])*scale+BigInt(fraction.slice(0,digits).padEnd(digits,'0')||'0');
 if(Number(fraction[digits]??0)>=5)units++;
 const decimal=digits?(units%scale).toString().padStart(digits,'0').replace(/0+$/,''):'';
 return (parts[1]&&units?'-':'')+(units/scale).toLocaleString('en-US')+(decimal?'.'+decimal:'');
}
export const amount=(value:unknown)=>number(value,2);
export function percent(value:unknown):string{return typeof value==='number'&&Number.isFinite(value)?`${number(value*100,2)}%`:missing;}
export function currency(value:unknown):string{return ({CNY:'人民币',USD:'美元',HKD:'港币',EUR:'欧元',JPY:'日元'} as Record<string,string>)[String(value)]??missing;}
export function status(value:unknown):string{return ({complete:'完成',completed:'完成',succeeded:'完成',success:'完成',failed:'失败',running:'运行中',idle:'空闲',stopped:'已停止',stopping:'停止中',unverified:'未验收',NO_QUALIFIED_STRATEGY:'未通过策略筛选',valid:'通过',invalid:'无效',filled:'已成交',partial:'部分成交',pending:'待处理',rejected:'未成交'} as Record<string,string>)[String(value)]??'状态未提供';}
export function errorMessage(value:unknown):string{
 const s=String(value).toLowerCase();
 if(/busy|running|忙|运行中/.test(s))return '已有任务运行中，请稍后重试';
 if(/interval.*fail|区间.*失败/.test(s))return '区间回测失败，请检查已完成的尝试';
 if(/permission|eperm|denied|权限/.test(s))return '服务无权读取所需文件';
 if(/not found|enoent|不存在|找不到/.test(s))return '所需文件或数据不存在';
 if(/network|connection|timeout|fetch failed|网络|连接|超时/.test(s))return '服务连接失败，请重试';
 if(/config|parameter|invalid|schema|配置|参数|日期/.test(s))return '配置或参数无效，请检查后重试';
 return '操作失败，请联系维护者检查服务记录';
}
export function security(value:unknown):string{
 if(typeof value!=='string'||!text(value))return missing;
 return /^(?:[A-Za-z]{0,6}[.:])?[A-Za-z\d]{1,12}$/.test(value)?value:missing;
}

export function tradeReason(value:unknown):string{
 return ({insufficient_cash_or_shares:'可用资金或可卖数量不足',suspended_or_zero_volume:'停牌或没有成交量',outside_limit_price:'价格超出涨跌停范围',weight_above_stock_limit:'超过单只证券仓位上限',position_above_stock_limit:'超过单只证券仓位上限',below_one_lot:'数量不足一手',order_above_adv_limit:'超过成交量限制',target_exit:'退出目标持仓'} as Record<string,string>)[String(value)]??(text(value)&&/[\u3400-\u9fff]/.test(String(value))?String(value):'原因未提供可读名称');
}
