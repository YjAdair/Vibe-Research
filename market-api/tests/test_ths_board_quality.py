import asyncio
import json

from app.datasources import ths_board


def _last_payload(data):
    return f'quotebridge_v4_line_bk_885710_01_last({json.dumps(data)})'


def _today_payload(fields):
    return f'quotebridge_v4_line_bk_885710_01_today({json.dumps({"bk_885710": fields})})'


class _Response:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


class _FixtureClient:
    def __init__(self, responses):
        self.responses = responses

    async def get(self, url):
        return self.responses[url.rsplit('/', 1)[-1]]


def _fetch(today_fields, historical_data=None):
    code = '885710'
    historical_data = historical_data or {
        'name': 'fixture',
        'data': '20260912,10,12,9,11,100,1000;20260913,11,13,10,12,110,1100',
    }
    client = _FixtureClient({
        'last.js': _Response(_last_payload(historical_data)),
        'today.js': _Response(_today_payload(today_fields)),
    })
    return asyncio.run(ths_board.fetch_board_bars(code, client=client))


def test_parse_bars_preserves_empty_positions_and_rejects_bad_values():
    payload = ';'.join([
        '20260910,10,11,9,10.5,100,1000,,,,0',
        '20260911,10,,9,10.5,100,1000',
        '20260912,2026-09-12,11,9,10,100,1000',
        '20260230,10,11,9,10,100,1000',
        '20260914,nan,11,9,10,100,1000',
        '20260915,10,inf,9,10,100,1000',
        '20260916,10,9,11,10,100,1000',
        '20260917,10,11,9,12,100,1000',
        '20260918,10,11,9,10,-1,1000',
        '20260919,10,11,9,10,100,-1',
    ])

    bars = ths_board._parse_bars(payload)

    assert bars == [{
        'date': '2026-09-10',
        'open': 10.0,
        'high': 11.0,
        'low': 9.0,
        'close': 10.5,
        'volume': 100.0,
        'amount': 1000.0,
    }]


def test_today_missing_fields_does_not_zero_fill_or_replace_complete_history():
    result = _fetch({
        '1': '20260913',
        '7': '11.5',
        '8': '12.5',
        '9': '11',
        '11': '12',
        '13': '110',
        # amount 19 is absent: the today row is incomplete and must be ignored
    })

    assert result['bars'][-1] == {
        'date': '2026-09-13',
        'open': 11.0,
        'high': 13.0,
        'low': 10.0,
        'close': 12.0,
        'volume': 110.0,
        'amount': 1100.0,
    }


def test_invalid_today_row_does_not_replace_valid_same_date_history():
    result = _fetch({
        '1': '20260913',
        '7': '11.5',
        '8': '10',
        '9': '11',
        '11': '12',
        '13': '110',
        '19': '1100',
    })

    assert result['bars'][-1]['date'] == '2026-09-13'
    assert result['bars'][-1]['close'] == 12.0
    assert result['bars'][-1]['amount'] == 1100.0
