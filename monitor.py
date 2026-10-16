import json, os, re, uuid
from pathlib import Path
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs
import requests
from playwright.sync_api import sync_playwright

ROOMS = {
    'スタンダード': 'f9eJwzNDOxMAIAAxABBg',
    'スタンダードプラス': '0aeJwztDQ1MgYAAxQBBQ',
    '和モダン': '1deJwzNDOxMAYAAxEBBw',
    'スーペリア': 'd8eJwzNDOxMAEAAxIBCA',
}
STATE = Path('state/state.json')
LOG = Path('state/latest.json')
JST = ZoneInfo('Asia/Tokyo')
READ = r"""() => {
 const c = document.querySelector('#hotel_calendar_content');
 if (!c) throw Error('calendar_missing');
 const match = c.innerText.match(/(\d{4})年\s*(\d{1,2})月/);
 if (!match) throw Error('month_missing');
 return {year:Number(match[1]), month:Number(match[2]), cells:
 [...c.querySelectorAll('td')].map(t=>({text:t.innerText.trim(), classes:t.className}))};
}"""

def url(evt):
    return ('https://reserva.be/aiikuretreat/reserve?mode=htl&evt_no='+evt+
            '&plan_no=73eJyzMDcwBwACIADX&room_num=1&people=1')

def read_room(page, evt):
    page.goto(url(evt), wait_until='domcontentloaded', timeout=60000)
    if parse_qs(urlparse(page.url).query).get('evt_no') != [evt]:
        raise ValueError('room URL mismatch')
        popup = page.locator('a.vacancy_popup')

    try:
        popup.wait_for(state='visible', timeout=30000)
    except Exception:
        print("=== PAGE DIAGNOSTICS START ===", flush=True)
        print("URL:", page.url, flush=True)
        print("TITLE:", page.title(), flush=True)
        print("POPUP LINK COUNT:", popup.count(), flush=True)
        print(
            "CALENDAR COUNT:",
            page.locator('#hotel_calendar_content').count(),
            flush=True,
        )

        try:
            body_text = page.locator('body').inner_text(timeout=5000)
            print("PAGE TEXT:", body_text[:2000], flush=True)
        except Exception:
            print("PAGE TEXT: could not read", flush=True)

        print("=== PAGE DIAGNOSTICS END ===", flush=True)
        raise

    popup.click()
    page.locator('#hotel_calendar_content').wait_for(state='visible', timeout=30000)
    page.wait_for_timeout(2500)
    found = {}
    for _ in range(24):
        raw = page.evaluate(READ)
        ym = (raw['year'], raw['month'])
        if ym in [(2026,10),(2026,11)]:
            cells = raw['cells']
            numbered = [c for c in cells if re.fullmatch(r'\d{1,2}', c['text'].split()[0] if c['text'] else '')]
            nums = [int(c['text'].split()[0]) for c in numbered]
            expected = 31 if ym[1] == 10 else 30
            if sorted(nums) != list(range(1,expected+1)):
                raise ValueError('unexpected calendar cells; refuse availability inference')
            dates=[]
            for c in numbered:
                if not {'complete','full'}.intersection(c['classes'].split()):
                    d=date(*ym,int(c['text'].split()[0]))
                    if d >= datetime.now(JST).date(): dates.append(d.isoformat())
            found[ym] = dates
        if len(found)==2: return sorted(set(sum(found.values(),[])))
        target = (2026,10) if (2026,10) not in found else (2026,11)
        direction='next' if ym < target else 'prev'
        arrow=page.locator('#hotel_calendar_content a.'+direction)
        if arrow.count()!=1 or not arrow.is_visible(): raise ValueError('month navigation unavailable')
        arrow.click()
        page.wait_for_function("old => { const s=document.querySelector('#hotel_calendar_content')?.innerText; const m=s?.match(/(\\d{4})年\\s*(\\d{1,2})月/); return m && (Number(m[1])!==old[0] || Number(m[2])!==old[1]); }", arg=list(ym),timeout=15000)
        page.wait_for_timeout(2000)
    raise ValueError('target months not verified')

def format_dates(items):
    days=sorted(date.fromisoformat(s) for s in items)
    def label(d): return f'{d.month}/{d.day}('+ '月火水木金土日'[d.weekday()]+')'
    groups=[]
    for d in days:
        if groups and d == groups[-1][1]+timedelta(days=1): groups[-1][1]=d
        else: groups.append([d,d])
    return '、'.join(label(a) if a==b else label(a)+'〜'+label(b) for a,b in groups) or 'なし'

def push(text):
    token=os.environ['LINE_CHANNEL_ACCESS_TOKEN']; recipient=os.environ['LINE_USER_ID']
    key=str(uuid.uuid4())
    for attempt in range(2):
        try:
            r=requests.post('https://api.line.me/v2/bot/message/push',
                headers={'Authorization':'Bearer '+token,'X-Line-Retry-Key':key},
                json={'to':recipient,'messages':[{'type':'text','text':text}]},timeout=30)
            if r.status_code==409 or r.ok: return
            raise RuntimeError('LINE HTTP '+str(r.status_code))
        except Exception:
            if attempt: raise

def main():
    old=json.loads(STATE.read_text()) if STATE.exists() else {}
    state=dict(old); results={}; errors={}
    dry=os.environ.get('DRY_RUN','false').lower()=='true'
    test=os.environ.get('TEST_NOTIFICATION','false').lower()=='true'
    if test and not dry: push('愛育空室監視：通知テストです。空きありの通知ではありません。')
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(locale='ja-JP',timezone_id='Asia/Tokyo')
        for room,evt in ROOMS.items():
            page=context.new_page()
            try:
                for attempt in range(2):
                    try:
                        dates=read_room(page,evt); break
                    except Exception:
                        if attempt: raise
                added=sorted(set(dates)-set(old.get(room,[])))
                checked=datetime.now(JST).isoformat(timespec='seconds')
                results[room]={'available':dates,'new':added,'checked_at_jst':checked,'notified':False}
                if added and not dry:
                    text=(f'🆕 【新規の空き】{room} {format_dates(added)}\n\n'
                          f'▼新たに空いた枠\n・{room}: {format_dates(added)}\n\n'
                          f'▼ここから予約サイトへ\n{url(evt)}\n\n'
                          f'▼この部屋タイプの現在の空き全体\n{format_dates(dates)}\n\n'
                          '空きは早い者勝ちです。予約手続きはご本人が行ってください。\n'
                          f'確認時刻: {checked}')
                    push(text)
                    results[room]['notified']=True
                if not dry: state[room]=dates
            except Exception as e:
                errors[room]=str(e)
            finally: page.close()
        browser.close()
    STATE.parent.mkdir(exist_ok=True)
    if not dry: STATE.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    report={'timestamp_jst':datetime.now(JST).isoformat(),'dry_run':dry,'results':results,'errors':errors}
    LOG.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    if errors: raise SystemExit(1)

if __name__=='__main__': main()
