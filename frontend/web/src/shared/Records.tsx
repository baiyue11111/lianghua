import type {RecordData} from '@display/workbench/port';
import {amount,day,missing,number,security,status,tradeReason} from './format';
export function RecordTable({rows}:{rows:RecordData[]}){
 if(!rows.length)return <div className="empty">没有成交记录</div>;
 const reasons=rows.some(row=>row.reason!=null);
 const columns=['日期','证券','买卖','数量','价格','金额','佣金','印花税','过户费','状态',...(reasons?['原因']:[])];
 return <div className="tablewrap"><table className="trades"><thead><tr>{columns.map(name=><th key={name}>{name}</th>)}</tr></thead><tbody>{rows.map((row,i)=>{
 const values=[day(row.trade_date),security(row.symbol),row.side==='buy'?'买入':row.side==='sell'?'卖出':missing,number(row.shares,6),amount(row.price),amount(row.gross_amount),amount(row.commission),amount(row.stamp_duty),amount(row.transfer_fee),status(row.status),...(reasons?[row.reason?tradeReason(row.reason):missing]:[])];
 return <tr key={i}>{values.map((value,j)=><td className="value" data-label={columns[j]} key={j}>{value}</td>)}</tr>;
 })}</tbody></table></div>;
}
export function Pages({offset,total,change,size=50}:{offset:number;total:number;change:(offset:number)=>void;size?:number}){
 if(total<=size)return null;
 return <nav className="table-bottom actions" aria-label="记录分页"><button disabled={offset===0} onClick={()=>change(Math.max(0,offset-size))}>上一页</button><span className="value">{offset+1}–{Math.min(offset+size,total)} / {total}</span><button disabled={offset+size>=total} onClick={()=>change(offset+size)}>下一页</button></nav>;
}
