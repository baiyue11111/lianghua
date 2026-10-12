"""Independent checks over persisted real runs; never imports strategy/ledger math."""
import argparse
from bisect import bisect_right
from calendar import monthrange
from collections import defaultdict
from datetime import date
from decimal import Decimal
import json
from math import sqrt
from pathlib import Path

import pyarrow.parquet as pq


def decimal(value):return Decimal(str(value))


def near(actual,expected):
    error=abs(decimal(actual)-decimal(expected))
    assert error<=Decimal('.000001'),f'{actual} differs from independently calculated {expected}'
    return error


def account_check(trace,closes,initial_cash):
    flows=defaultdict(lambda:Decimal(0))
    for fill in trace['fills']:
        if fill['status']!='filled':continue
        gross=decimal(fill['gross_amount']);fees=decimal(fill['commission'])+decimal(fill['stamp_duty'])
        flows[fill['trade_date']]+=(-gross if fill['side']=='buy' else gross)-fees
    for event in trace['cash_events']:
        if event['kind'] in ('dividend','dividend_tax','transfer_fee'):
            flows[event['day']]+=decimal(event['cash_delta'])
    holdings=dict(trace['holdings']);cash=decimal(initial_cash);error=Decimal(0)
    for point in trace['equity']:
        day=point['day'];cash+=flows[day]
        market=sum((decimal(lot['shares'])*decimal(closes[(lot['symbol'],day)]) for lot in holdings[day]),Decimal(0))
        error=max(error,near(point['cash'],cash),near(point['market_value'],market),
                  near(point['equity'],cash+market+decimal(point['receivables'])-decimal(point['liabilities'])))
        assert cash>=0,'cash must never be negative'
    return {'checked_sessions':len(trace['equity']),'maximum_error':float(error)}


def metrics_check(equity,reported):
    values=[float(value) for value in equity]
    peak=values[0];drawdown=0.
    for value in values:
        peak=max(peak,value);drawdown=min(drawdown,value/peak-1)
    returns=[b/a-1 for a,b in zip(values,values[1:])]
    result={'total_return':values[-1]/values[0]-1,'max_drawdown':drawdown}
    if len(returns)>1:
        mean=sum(returns)/len(returns)
        variance=sum((value-mean)**2 for value in returns)/(len(returns)-1)
        result['sharpe']=mean/sqrt(variance)*sqrt(252) if variance else None
    for key,value in result.items():
        if key not in reported:continue
        if value is None:assert reported[key] is None
        else:near(reported[key],value)
    return result


def targets_check(trace,folder,params,start):
    prices=defaultdict(list)
    raw=pq.read_table(folder/'daily/bars.parquet').to_pylist()
    factors={}
    for path in (folder/'factors').glob('*.json'):
        payload=json.loads(path.read_bytes())['raw_response']
        values=[dict(zip(payload['fields'],row)) for row in payload['rows']]
        factors[payload['code'].split('.')[-1]]=([row['dividOperateDate'] for row in values],[float(row['backAdjustFactor']) for row in values])
    by_day=defaultdict(dict)
    for row in raw:
        if row['volume']>0 and row['amount']>0:
            dates,amounts=factors[row['code']];index=bisect_right(dates,row['date'])-1
            assert index>=0,'explicit initial factor required'
            by_day[row['date']][row['code']]=row['close']*amounts[index]
    expected=None;decisions=0
    for point,target in zip(trace['equity'],trace['targets'],strict=True):
        day=point['day']
        for symbol,value in by_day[day].items():prices[symbol].append(value)
        if day<start:
            assert target is None,'independent strategy may not trade during warm-up'
            continue
        selected=[]
        for symbol,values in sorted(prices.items()):
            assert len(values)>=params['slow_window']
            short=sum(values[-params['fast_window']:])/params['fast_window']
            long=sum(values[-params['slow_window']:])/params['slow_window']
            if short>long and params['exposure']>0:selected.append(symbol)
        selected=tuple(selected)
        if selected==expected:assert target is None,'unchanged set must not rebalance'
        else:
            assert target is not None,'new selection must produce a target'
            weights={symbol:params['exposure']/len(selected) for symbol in selected}
            assert set(target['weights'])==set(weights)
            for symbol,value in weights.items():near(target['weights'][symbol],value)
            expected=selected;decisions+=1
    return {'checked_decisions':decisions,'checked_sessions':len(trace['targets'])}


def tax_rate(acquired,disposed):
    # Natural-month/year boundaries, calculated independently of production.
    acquired=date.fromisoformat(acquired);disposed=date.fromisoformat(disposed)
    year,month=divmod(acquired.year*12+acquired.month,12)
    month+=1
    one_month=date(year,month,min(acquired.day,monthrange(year,month)[1]))
    one_year=date(acquired.year+1,acquired.month,min(acquired.day,monthrange(acquired.year+1,acquired.month)[1]))
    return Decimal('.2') if disposed<=one_month else Decimal('.1') if disposed<=one_year else Decimal(0)


def actions_from_source(folder):
    manifest=json.loads((folder/'manifest.json').read_bytes());actions={}
    for fact in manifest['action_facts']:
        payload=json.loads((folder/fact['source_file']).read_bytes())['raw_response']
        row=dict(zip(payload['fields'],payload['rows'][fact['source_row']],strict=True))
        assert decimal(row['dividStocksPs'] or '0')==0 and decimal(row['dividReserveToStockPs'] or '0')==0,'this independent real sample uses cash distributions'
        action={'id':fact['distribution_id'],'symbol':row['code'].split('.')[-1],'registration':row['dividRegistDate'],
                'operation':row['dividOperateDate'],'payment':row['dividPayDate'],'cash':row['dividCashPsBeforeTax']}
        if action['id'] in actions:assert actions[action['id']]==action,'conflicting declared plan'
        actions[action['id']]=action
    return list(actions.values())


def tax_check(trace,actions):
    # Independent daily-net FIFO lots; rights come from actual registration holdings.
    lots=[];pending=[];claims=[];income=defaultdict(lambda:Decimal(0));tax=defaultdict(lambda:Decimal(0))
    fills=defaultdict(list);events=defaultdict(list);registrations=defaultdict(list)
    for fill in trace['fills']:
        if fill['status']=='filled':fills[fill['trade_date']].append(fill)
    for action in actions:registrations[action['registration']].append(action)
    for event in trace['cash_events']:events[event['day']].append(event)
    checks=0
    for point in trace['equity']:
        day=point['day'];net=defaultdict(int)
        for fill in fills[day]:net[fill['symbol']]+=fill['shares']*(1 if fill['side']=='buy' else -1)
        for symbol,change in net.items():
            if change>0:lots.append({'symbol':symbol,'acquired':day,'shares':change,'rights':[]})
            elif change<0:
                remaining=-change
                for lot in lots:
                    count=min(lot['shares'],remaining) if lot['symbol']==symbol else 0
                    for right in lot['rights']:
                        pending.append({'due':max(day,right['payment']),'operation':right['operation'],
                            'amount':decimal(right['cash'])*count*tax_rate(lot['acquired'],day)})
                    lot['shares']-=count;remaining-=count
                assert remaining==0,'FIFO disposal must match held shares'
                lots=[lot for lot in lots if lot['shares']]
        for payment in pending:
            if payment['due']<=day:
                tax[day]+=payment['amount'];payment['amount']=Decimal(0)
        receivable=sum((amount for action,amount in claims if action['operation']<=day<action['payment']),Decimal(0))
        for action,amount in claims:
            if action['payment']==day:income[day]+=amount
        potential=sum((decimal(right['cash'])*lot['shares']*tax_rate(lot['acquired'],day)
            for lot in lots for right in lot['rights'] if right['operation']<=day),Decimal(0))
        fixed=sum((payment['amount'] for payment in pending if payment['operation']<=day),Decimal(0))
        near(point['receivables'],receivable);near(point['liabilities'],potential+fixed)
        near(-sum((decimal(e['cash_delta']) for e in events[day] if e['kind']=='dividend_tax'),Decimal(0)),tax[day])
        near(sum((decimal(e['cash_delta']) for e in events[day] if e['kind']=='dividend'),Decimal(0)),income[day])
        for action in registrations[day]:
            quantity=sum(lot['shares'] for lot in lots if lot['symbol']==action['symbol'])
            claims.append((action,decimal(action['cash'])*quantity))
            for lot in lots:
                if lot['symbol']==action['symbol']:lot['rights'].append(action)
        checks+=1
    return {'checked_sessions':checks,'source_distributions':len(actions),'gross_dividends':float(sum(income.values())),
            'paid_dividend_tax':float(sum(tax.values()))}


def check(root,proof,folder):
    raw=pq.read_table(folder/'daily/bars.parquet').to_pylist()
    closes={(row['code'],row['date']):row['close'] for row in raw};report={}
    for name in ('a','b'):
        run=proof[name];path=root/Path(run['manifest_file']).parent
        trace=json.loads((path/'trace.json').read_bytes())
        values=[point['equity'] for point in run['equity']]
        report[name]={'run_id':run['run_id'],'params':run['parameters'],
            'targets':targets_check(trace,folder,run['parameters'],proof['frozen']['start']),
            'account':account_check(trace,closes,proof['frozen']['initial_cash']),
            'tax':tax_check(trace,actions_from_source(folder)),
            'metrics':metrics_check(values,run['metrics'])}
    assert proof['a']['data_hash']==proof['b']['data_hash']
    assert proof['a']['code_hash']==proof['b']['code_hash']
    assert proof['a']['execution_hash']==proof['b']['execution_hash']
    assert proof['a']['config_hash']!=proof['b']['config_hash']
    assert proof['a']['equity']!=proof['b']['equity'],'frozen window change must affect this held-out sample'
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--proof',required=True);parser.add_argument('--data',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();root=Path.cwd();proof=json.loads(Path(args.proof).read_bytes())
    result=check(root,proof,Path(args.data));Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print('PASS independent targets, FIFO dividend tax, account, NAV and metrics for both held-out runs')
