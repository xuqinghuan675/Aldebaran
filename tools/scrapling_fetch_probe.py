from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urljoin


def main() -> int:
    parser = argparse.ArgumentParser(description='Fetch one public page with Scrapling.')
    parser.add_argument('--url', required=True)
    parser.add_argument('--mode', choices=('static', 'dynamic', 'stealth'), default='static')
    parser.add_argument('--timeout', type=int, default=25)
    parser.add_argument('--selectors-json', default='')
    parser.add_argument('--json', action='store_true', dest='as_json')
    args = parser.parse_args()

    selectors = _parse_selectors(args.selectors_json)
    payload = fetch(args.url, mode=args.mode, timeout=args.timeout, selectors=selectors)
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if payload.get('ok') else 1

    if payload.get('ok'):
        print('PASS static fetch' if args.mode == 'static' else f'PASS {args.mode} fetch')
        print('PASS title extracted' if payload.get('title') else 'WARN title missing')
        print('PASS text extracted' if payload.get('text') else 'WARN text missing')
        if payload.get('items'):
            print(f"PASS items extracted: {len(payload['items'])}")
        return 0
    print(f"FAIL {args.mode} fetch: {payload.get('error')}")
    return 1


def fetch(url: str, *, mode: str, timeout: int, selectors: dict[str, str] | None = None) -> dict:
    try:
        page = _fetch_page(url, mode=mode, timeout=timeout)
        title = _first_text(page, selectors.get('page_title_selector') if selectors else 'title::text')
        body_text = _body_text(page)
        items = _extract_items(page, url, selectors or {})
        return {
            'ok': True,
            'url': url,
            'mode': mode,
            'title': title,
            'text': body_text[:8000],
            'items': items,
            'error': '',
        }
    except Exception as exc:
        return {
            'ok': False,
            'url': url,
            'mode': mode,
            'title': '',
            'text': '',
            'items': [],
            'error': f'{type(exc).__name__}: {exc}',
        }


def _fetch_page(url: str, *, mode: str, timeout: int):
    from scrapling import DynamicFetcher, Fetcher, StealthyFetcher

    if mode == 'dynamic':
        return DynamicFetcher.fetch(url, timeout=timeout)
    if mode == 'stealth':
        return StealthyFetcher.fetch(url, timeout=timeout)
    return Fetcher.get(url, timeout=timeout)


def _extract_items(page, base_url: str, selectors: dict[str, str]) -> list[dict]:
    title_selector = selectors.get('title_selector')
    url_selector = selectors.get('url_selector')
    date_selector = selectors.get('date_selector')
    summary_selector = selectors.get('summary_selector')
    if title_selector:
        titles = _all_text(page, title_selector)
        urls = _all_text(page, url_selector) if url_selector else []
    else:
        titles = _all_text(page, 'a::text')
        urls = _all_text(page, 'a::attr(href)')
    dates = _all_text(page, date_selector) if date_selector else []
    summaries = _all_text(page, summary_selector) if summary_selector else []

    items: list[dict] = []
    for idx, title in enumerate(titles[:30]):
        clean_title = _clean(title)
        if not clean_title:
            continue
        href = urls[idx] if idx < len(urls) else ''
        items.append({
            'title': clean_title,
            'date': _clean(dates[idx]) if idx < len(dates) else '',
            'url': urljoin(base_url, href) if href else '',
            'summary': _clean(summaries[idx]) if idx < len(summaries) else '',
        })
    return items


def _body_text(page) -> str:
    values = _all_text(page, 'body ::text')
    return _clean(' '.join(values))


def _first_text(page, selector: str | None) -> str:
    if not selector:
        return ''
    try:
        value = page.css(selector).get(default='')
    except Exception:
        return ''
    return _clean(value)


def _all_text(page, selector: str | None) -> list[str]:
    if not selector:
        return []
    try:
        return [_clean(value) for value in page.css(selector).getall() if _clean(value)]
    except Exception:
        return []


def _parse_selectors(value: str) -> dict[str, str]:
    if not value:
        return {}
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(val) for key, val in data.items() if val}


def _clean(value) -> str:
    return ' '.join(str(value or '').split())


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    raise SystemExit(main())
