from tools.audit_hotspot_year import summarize


def test_missing_and_non_finite_not_counted_but_zero_is_valid():
    days = ['2026-01-05', '2026-01-06']
    rows = {('BK1', days[0]): {'pct': 0}, ('BK2', days[0]): {'pct': None},
            ('BK1', days[1]): {'pct': float('nan')}}
    result = summarize(rows, days, {'pct': ['pct']})['pct']
    assert result['count_by_date'] == {days[0]: 1, days[1]: 0}
    assert result['missing_dates'] == [days[1]]
    assert result['observed_codes_with_every_day'] == 0


def test_one_board_does_not_prove_full_universe():
    days = ['2026-01-05', '2026-01-06']
    rows = {('BK1', d): {'pct': 2} for d in days}
    rows[('BK2', days[0])] = {'pct': 3}
    result = summarize(rows, days, {'pct': ['pct']})['pct']
    assert result['days_with_any_data'] == 2
    assert result['observed_codes'] == 2
    assert result['observed_codes_with_every_day'] == 1
