"""Broadcast collection. Shared tasks persist records and original pages."""
from datetime import date, timedelta
from urllib.request import Request, urlopen

from .data import CollectionError


def http(url):
    request = Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urlopen(request, timeout=30) as response:
        return response.read().decode('utf-8')


class News:
    def __init__(self, config, transport=http):
        self.config = config
        self.transport = transport

    def collect(self, request):
        from bs4 import BeautifulSoup
        start, end = date.fromisoformat(request['start']), date.fromisoformat(request['end'])
        if start > end:
            raise ValueError('start must not exceed end')
        records, raw = [], []
        day = start
        try:
            while day <= end:
                url = f'https://cn.govopendata.com/xinwenlianbo/{day:%Y%m%d}/'
                page = self.transport(url)
                raw.append({'url': url, 'request': {'date': day.isoformat()}, 'response': page})
                soup = BeautifulSoup(page, 'html.parser')
                articles = soup.select('article.content-section')
                if not articles:
                    raise ValueError('broadcast page contains no articles')
                for seq, article in enumerate(articles, 1):
                    heading = article.find(['h1', 'h2', 'h3'])
                    body = article.select_one('.content-body')
                    if heading is None or body is None:
                        raise ValueError('incomplete broadcast article')
                    headline = heading.get_text(strip=True)
                    content = '\n'.join(p.get_text(strip=True) for p in body.find_all('p'))
                    if not headline or not content:
                        raise ValueError('empty broadcast article')
                    if request.get('keyword', '') not in headline + content:
                        continue
                    records.append({'date': day.isoformat(), 'seq': seq,
                                    'section_id': (heading.get('id') or article.get('id') or '').removeprefix('section-'),
                                    'headline': headline, 'content': content, 'raw_page': len(raw) - 1})
                day += timedelta(days=1)
        except Exception as error:
            raise CollectionError(str(error), raw) from error
        return {'records': records, 'raw': raw, 'data_as_of': end.isoformat(),
                'scope': {'start': start.isoformat(), 'end': end.isoformat(), 'keyword': request.get('keyword', '')}}
