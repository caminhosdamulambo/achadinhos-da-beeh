"""Gera RSS para o Metricool. O endereço privado vem de SHOPEE_FEED_URL."""
import csv
import datetime as dt
import email.utils
import json
import heapq
from zoneinfo import ZoneInfo
import math
import os
from pathlib import Path
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
PAGE = 'https://www.facebook.com/profile.php?id=61589221185748'
AFFILIATE = '18327641093'
INTERVAL = 48 * 3600


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else 0
    except (TypeError, ValueError):
        return 0


def select(path, seen, current_id, zap_seen=(), queued_ids=()):
    best, best_score, current = None, -1, None
    zap_seen, queued_ids = set(zap_seen), set(queued_ids)
    heap, queued, counter = [], {}, 0
    with open(path, encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        required = {'itemid', 'title', 'sale_price', 'item_rating', 'shop_rating', 'product_link', 'discount_percentage'}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError('Catálogo inválido')
        for row in reader:
            origin = row['product_link']
            parsed = urllib.parse.urlparse(origin)
            if parsed.scheme != 'https' or parsed.hostname != 'shopee.com.br' or not parsed.path.startswith('/product/'):
                continue
            price, rating, shop, discount = (number(row.get(k)) for k in ('sale_price', 'item_rating', 'shop_rating', 'discount_percentage'))
            if not 5 <= price <= 100 or min(rating, shop) < 4.7 or discount < 15:
                continue
            candidate = {k: row.get(k, '') for k in ('itemid', 'title', 'image_link', 'product_link', 'price', 'sale_price')}
            if row['itemid'] == current_id:
                current = candidate
            score = discount + rating * 5 + shop * 3
            if row['itemid'] in queued_ids:
                queued[row['itemid']] = candidate.copy()
            if row['itemid'] not in zap_seen and candidate['image_link'].startswith('https://'):
                counter += 1
                heapq.heappush(heap, (score, counter, candidate.copy()))
                if len(heap) > 1000:
                    heapq.heappop(heap)
            if row['itemid'] not in seen and score > best_score:
                best, best_score = candidate, score
    return best, current, [x[2] for x in sorted(heap, reverse=True)], queued


def write_feed(item, filename="feed.xml"):
    items = item if isinstance(item, list) else ([item] if item else [])
    rss = ET.Element('rss', version='2.0')
    channel = ET.SubElement(rss, 'channel')
    for key, value in {'title': 'Achadinhos da Beeh', 'link': PAGE, 'description': 'Ofertas selecionadas do catálogo Shopee', 'language': 'pt-br'}.items():
        ET.SubElement(channel, key).text = value
    for item in items:
        entry = ET.SubElement(channel, 'item')
        title = ' '.join(item['title'].split())[:300].replace("'", '’')
        link = 'https://shope.ee/an_redir?' + urllib.parse.urlencode({'origin_link': item['product_link'], 'affiliate_id': AFFILIATE})
        sale, original = number(item.get('sale_price')), number(item.get('price'))
        money = lambda amount: 'R$ ' + f'{amount:.2f}'.replace('.', ',')
        prices = []
        if sale > 0:
            prices.append('Valores do catálogo:')
            prices.append('💰 Preço: ' + money(sale))
            
                
        description = '\n'.join(['🎯 ACHADINHO DA BEEH!', '', '🛍️ ' + title, '', *prices, '', '🔎 Confira preço, estoque, frete e opções no link antes de comprar.', '', 'Publicidade'])
        for key, value in {'title': title, 'description': description, 'link': link, 'pubDate': email.utils.format_datetime(dt.datetime.fromtimestamp(item['created'], dt.timezone.utc))}.items():
            ET.SubElement(entry, key).text = value
        ET.SubElement(entry, 'guid', isPermaLink='false').text = item['guid']
        image = item.get('image_link', '')
        if urllib.parse.urlparse(image).scheme == 'https':
            ET.SubElement(entry, 'enclosure', url=image, type='image/jpeg', length='0')
    output = ROOT / 'public'
    output.mkdir(exist_ok=True)
    ET.ElementTree(rss).write(output / filename, encoding='utf-8', xml_declaration=True)
    (output / '_headers').write_text('/feed*.xml\n  Content-Type: application/rss+xml; charset=utf-8\n  Cache-Control: no-cache\n', encoding='utf-8')


def update_zap(state, candidates, refreshed, now):
    # Separate queue: Facebook keeps feed.xml and its original interval.
    seen = set(state.get('zap_seen', state['seen']))
    items = []
    for old_item in state.get('zap_items', []):
        if old_item['created'] + 300 <= now:
            continue
        fresh = refreshed.get(old_item['itemid'])
        if fresh and fresh.get('image_link', '').startswith('https://'):
            fresh.update(created=old_item['created'], guid=old_item['guid'])
            items.append(fresh)
    occupied = {int(x['created']) for x in items}
    candidates = iter(x for x in candidates if x['itemid'] not in seen)
    start = int(now // 300) * 300
    for slot in range(start, start + 36 * 3600, 300):
        hour = dt.datetime.fromtimestamp(slot, ZoneInfo('America/Sao_Paulo')).hour
        if not 8 <= hour < 22 or slot in occupied:
            continue
        candidate = next(candidates, None)
        while candidate and candidate['itemid'] in seen:
            candidate = next(candidates, None)
        if candidate is None:
            break
        candidate.update(created=slot, guid='beeh-zap-' + candidate['itemid'] + '-' + str(slot))
        items.append(candidate)
        seen.add(candidate['itemid'])
    state['zap_seen'] = sorted(seen)
    state['zap_items'] = sorted(items, key=lambda x: x['created'])
    write_feed(state['zap_items'], 'feed-zap.xml')


def main():
    now = time.time()
    state_file = ROOT / 'estado.json'
    state = json.loads(state_file.read_text(encoding='utf-8')) if state_file.exists() else {'seen': ['20998290033'], 'item': None}
    old = state.get('item')
    url = os.environ.get('SHOPEE_FEED_URL', '')
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname != 'affiliate.shopee.com.br':
        raise ValueError('Configure o segredo SHOPEE_FEED_URL')
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / 'catalogo.csv'
        with urllib.request.urlopen(url, timeout=60) as response, path.open('wb') as output:
            if urllib.parse.urlparse(response.url).scheme != 'https':
                raise ValueError('Redirecionamento inválido')
            size = 0
            while block := response.read(1024 * 1024):
                size += len(block)
                if size > 600 * 1024 * 1024:
                    raise ValueError('Catálogo muito grande')
                output.write(block)
        best, current, candidates, refreshed = select(path, set(state['seen']), old['itemid'] if old else '', state.get('zap_seen', state['seen']), [x['itemid'] for x in state.get('zap_items', [])])
    if not old or not current or now - old['created'] >= INTERVAL:
        if best:
            best.update(created=now, guid='beeh-' + best['itemid'] + '-' + str(int(now)))
            state['item'] = best
            state['seen'].append(best['itemid'])
        else:
            state['item'] = None
    elif current:
        current.update(created=old['created'], guid=old['guid'])
        state['item'] = current
    else:
        # Não substitui antes de 48h; retira oferta que perdeu os filtros.
        state['item'] = old
    item = state.get('item')
    available = item and (not old or item['guid'] != old['guid'] or current)
    write_feed(item if available and now - item['created'] <= 36 * 3600 else None)
    update_zap(state, candidates, refreshed, now)
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('RSS atualizado. Links experimentais: comissão ainda não confirmada.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        write_feed(None)
        write_feed(None, "feed-zap.xml")
        # Não registra o URL privado nem a mensagem da biblioteca HTTP.
        print('Falha ao atualizar o catálogo. RSS esvaziado; confira o segredo e o serviço Shopee.')
        raise SystemExit(1)
