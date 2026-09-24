"""2025预热/2026交易制度：规则版本与逐证券状态证据分离，不从名称或代码猜测。"""
from datetime import date as Date
from app.services import ml_r1

SOURCES = {
    'sh_main': 'https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml',
    'sz_main': 'https://investor.szse.cn/lawrules/rule/trade/t20260424_620190.html',
    'chinext': 'https://investor.szse.cn/institute/rules/t20200807_580310.html',
    'star': 'https://star.sse.com.cn/star/media/news/c/c_20190719_4866789.shtml',
    'bse': 'https://www.bse.cn/jygl_list/200028217.html',
}


def resolve(day, evidence):
    Date.fromisoformat(day)
    missing={'status':'missing_input','rule':None,'reasons':[]}
    def unavailable(reason):
        return {**missing,'reasons':[reason]}
    if not '2025-01-01' <= day < '2027-01-01':
        return unavailable('rule_date_outside_verified_window')
    if (not isinstance(evidence, dict) or evidence.get('verified') is not True or evidence.get('date') != day or not evidence.get('source')
            or not evidence.get('evidence_ref') or not ml_r1.known_before(evidence.get('known_at'),day+'T09:30:00')):
        return unavailable('missing_security_rule_evidence')
    board=evidence.get('board');event=evidence.get('event')
    if board not in SOURCES or event not in ('ordinary','ipo','delisting','relisting'):
        return unavailable('unknown_board_or_listing_event')
    session=evidence.get('event_trading_day')
    if event!='ordinary' and (type(session) is not int or session<1):
        return unavailable('missing_event_trading_day')
    if (event=='relisting' and board not in ('sh_main','sz_main')) or (event=='delisting' and board=='star'):
        return unavailable('unverified_special_event_rule')
    unlimited=(event=='ipo' and session<=(1 if board=='bse' else 5)) or (event in ('delisting','relisting') and session==1)
    risk=evidence.get('risk_warning')
    if board in ('sh_main','sz_main') and day<'2026-07-06' and event!='delisting' and not unlimited and type(risk) is not bool:
        return unavailable('missing_dated_risk_warning')
    rate=.3 if board=='bse' else .2 if board in ('chinext','star') else .05 if risk is True and day<'2026-07-06' and event!='delisting' else .1
    rule_source=SOURCES[board]
    if day<'2026-07-06':
        if board=='sh_main':
            rule_source='https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20150912_3988629.shtml' if risk is True and event!='delisting' else 'https://www.sse.com.cn/home/component/news/c/c_20230201_5715622.shtml'
        elif board=='sz_main':
            rule_source='https://investor.szse.cn/institute/bookshelf/manualseriesbook/P020230403389861343977.pdf' if risk is True else 'https://investor.szse.cn/institute/rules/t20230621_601279.html'
        elif board=='bse':
            rule_source='https://www.bse.cn/jygl_list/200010919.html'
    rule={'verified':True,'source':evidence['source'],'evidence_ref':evidence['evidence_ref'],
          'known_at':evidence['known_at'],'effective_from':day,'effective_to':Date.fromordinal(Date.fromisoformat(day).toordinal()+1).isoformat(),
          'applicable':not unlimited,'rate':None if unlimited else rate,'tick':'0.01',
          'minimum_move':board in ('sz_main','chinext'),
          'rule_version':'a_share_2026_v1','board':board,'event':event,'rule_source':rule_source,
          'regime_period':'2026-07-06+' if day>='2026-07-06' else 'pre-2026-07-06'}
    return {'status':'verified','rule':rule,'reasons':[]}
