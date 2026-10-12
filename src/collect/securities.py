"""Security-list input without import-time requests or private storage."""
import json
import re
from .data import CollectionError


def listed_names():
    import akshare
    return akshare.stock_info_a_code_name()


class Securities:
    def __init__(self, config, provider=None):
        self.config = config
        self.provider = provider

    def collect(self, request):
        if self.provider is None:
            import akshare as provider
        else:
            provider = self.provider
        raw, records = [], []
        tables = (
            ('SH', provider.stock_info_sh_delist, '全部',
             {'公司代码': 'code', '公司简称': 'name', '上市日期': 'list_date', '暂停上市日期': 'delist_date'}),
            ('SZ', provider.stock_info_sz_delist, '终止上市公司',
             {'证券代码': 'code', '证券简称': 'name', '上市日期': 'list_date', '终止上市日期': 'delist_date'}),
        )
        try:
            for exchange, query, symbol, names in tables:
                frame = query(symbol=symbol)
                rows = json.loads(frame.to_json(orient='records', force_ascii=False, date_format='iso'))
                raw.append({'provider': 'exchange', 'request': {'exchange': exchange, 'symbol': symbol}, 'response': rows})
                for row in rows:
                    value = {name: row[column] for column, name in names.items()}
                    code = value['code']
                    if not isinstance(code, str) or not re.fullmatch(r'\d{6}', code):
                        raise ValueError('invalid official security identity')
                    value['exchange'] = exchange
                    value['bs_code'] = exchange.lower() + '.' + code
                    value['raw_page'] = len(raw) - 1
                    if not request['start'] <= value['delist_date'] <= request['end']:
                        continue
                    if request.get('codes') and value['bs_code'] not in request['codes']:
                        continue
                    records.append(value)
            keys = [(row['exchange'], row['code']) for row in records]
            if len(keys) != len(set(keys)):
                raise ValueError('duplicate official securities')
        except Exception as error:
            raise CollectionError(str(error), raw) from error
        return {'records': records, 'raw': raw, 'data_as_of': request['end'],
                'scope': {'start': request['start'], 'end': request['end'], 'codes': request.get('codes', [])}}
