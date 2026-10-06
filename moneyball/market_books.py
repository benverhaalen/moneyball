"""Cache a manifest of documented read-only order-book requests."""
import argparse
import json
from pathlib import Path
import time
from urllib.parse import urlparse
from .market_acquisition import Collector, decode_json
from .warehouse import canonical
from .market_quotes import number, quote_band


def normalize_book(item):
    """Read actual levels, independent of provider ordering; ignore zero size."""
    sid=item['source_id'];data=item['data'];receipt=item['receipt']
    def level(price,size):
        p,q=number(price),number(size)
        if p is None or q is None or not 0<=p<=1 or q<0:raise ValueError('Invalid book level')
        return (p,q)
    if sid=='polymarket_global_public':
        if item.get('token_id') and str(data['asset_id'])!=str(item['token_id']):raise ValueError('Wrong outcome token')
        bids=[level(x['price'],x['size']) for x in data['bids']]
        asks=[level(x['price'],x['size']) for x in data['asks']]
        quote_time=float(data['timestamp'])/1000
    elif sid=='polymarket_us_public':
        d=data['marketData']
        if d.get('marketSlug') and '/'+d['marketSlug']+'/' not in item['url']:raise ValueError('Wrong US market')
        def us(rows):
            result=[]
            for x in rows:
                if x['px'].get('currency')!='USD':raise ValueError('Non-USD US quote')
                result.append(level(x['px']['value'],x['qty']))
            return result
        bids=us(d.get('bids',[]));asks=us(d.get('offers',[]))
        from .warehouse import timestamp
        quote_time=timestamp(d['transactTime']) if d.get('transactTime') else None
    elif sid=='kalshi_public':
        d=data['orderbook_fp'];bids=[level(*x) for x in d.get('yes_dollars',[])]
        # Kalshi publishes bids for each outcome. A No bid implies a Yes ask.
        asks=[(1-p,q) for p,q in [level(*x) for x in d.get('no_dollars',[])]]
        quote_time=None
    else:raise ValueError('Unsupported order-book source')
    bids=sorted((x for x in bids if x[1]>0),reverse=True)
    asks=sorted(x for x in asks if x[1]>0)
    def average_for_size(levels,size):
        remaining=size;total=0
        for price,qty in levels:
            take=min(remaining,qty);remaining-=take;total+=take*price
            if remaining<=1e-10:return total/size
        return None
    snapshots={}
    for qty in (1,10,100):
        b=average_for_size(bids,qty);a=average_for_size(asks,qty)
        snapshots[str(qty)]={**quote_band(b,a),'shares':qty}
    return {'source_id':sid,'market_id':item['market_id'],
        'observed_at':receipt['observed_at'],'source_book_time':quote_time,
        'raw_id':receipt['id'],'raw_sha256':receipt['sha256'],'url':item['url'],
        'top':quote_band(bids[0][0] if bids else None,asks[0][0] if asks else None),
        'best_bid_size':bids[0][1] if bids else None,'best_ask_size':asks[0][1] if asks else None,
        'size_sensitivity':snapshots,
        'interpretation':'Pre-fee displayed order-book depth. The size sensitivity is a quote-quality diagnostic, not independent observations or a recommendation to trade.'}


def acquire(manifest,root='.moneyball/markets'):
    items=json.loads(Path(manifest).read_text());c=Collector(root)
    allowed={'polymarket_global_public':('clob.polymarket.com',),
             'polymarket_us_public':('gateway.polymarket.us',),
             'kalshi_public':('external-api.kalshi.com',)}
    results=[]
    for item in items:
        u=urlparse(item['url'])
        if u.scheme!='https' or u.hostname not in allowed[item['source_id']] or not (u.path.endswith('/book') or u.path.endswith('/orderbook')):
            raise ValueError('Only verified public order-book hosts and paths allowed')
        try:
            receipt=c.fetcher.fetch(item['source_id'],item['url'],ttl=300)
            data=decode_json(c.warehouse.raw_bytes(receipt['id']))
            results.append({**item,'receipt':receipt,'data':data})
            print(canonical({'market_id':item['market_id'],'raw_id':receipt['id'],'keys':list(data)}),flush=True)
        except Exception as e:
            results.append({**item,'error':str(e),'observed_at':time.time()})
            print(canonical({'market_id':item['market_id'],'error':str(e)}),flush=True)
    p=Path(root)/'runs'/(str(int(time.time()*1000))+'-orderbooks.json')
    p.write_text(canonical({'observed_at':time.time(),'requests':items,'results':results})+'\n')
    return {'path':str(p),'successful':sum('data' in r for r in results),'failed':sum('error' in r for r in results)}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('manifest');p.add_argument('--root',default='.moneyball/markets')
    a=p.parse_args();print(canonical(acquire(a.manifest,a.root)))
