import pykeybasebot.types.chat1 as chat1
from pykeybasebot import Bot
from hashlib import sha256
import websocket
import threading
import requests
import asyncio
import base64
import json
import time
import config

issues = {}
last_sent_alert = time.time()
keybase_bot = None
if config.KEYBASE_BOT_KEY:
    keybase_bot = Bot(
        username=config.KEYBASE_BOT_USERNAME,
        paperkey=config.KEYBASE_BOT_KEY,
        handler=None
    )


def how_long(ts):
    duration = time.time() - ts
    if duration > 24 * 60 * 60:
        int_part = int(duration / (24 * 60 * 60))
        str_part = 'day' if int_part == 1 else 'days'
    elif duration > 60 * 60:
        int_part = int(duration / (60 * 60))
        str_part = 'hour' if int_part == 1 else 'hours'
    elif duration > 60:
        int_part = int(duration / 60)
        str_part = 'minute' if int_part == 1 else 'minutes'
    else:
        return ''
    return f'since {int_part} {str_part} ago'


def alert(issue):
    global last_sent_alert
    if issue['resolved']:
        msg = issue['message']
    else:
        msg = f"{issue['message']} {how_long(issue['started_at'])}"
    print(time.strftime('%a, %d %b %Y %H:%M:%S', time.gmtime()), msg)
    if config.KEYBASE_BOT_KEY:
        try:
            channel = chat1.ChatChannel(**config.KEYBASE_BOT_CHANNEL)
            asyncio.run(keybase_bot.chat.send(channel, msg))
            keybase_done = True
        except Exception as e:
            print('keybase error', e)
            keybase_done = False
    if config.TELEGRAM_BOT_KEY:
        try:
            payload = json.dumps(
                {'chat_id': config.TELEGRAM_BOT_CHANNEL, 'text': msg})
            headers = {'content-type': 'application/json',
                       'cache-control': 'no-cache'}
            url = f'https://api.telegram.org/bot{config.TELEGRAM_BOT_KEY}/sendMessage'
            requests.post(url, data=payload, headers=headers)
            telegram_done = True
        except Exception as e:
            print('telegram error', e)
            telegram_done = False
    if keybase_done or telegram_done:
        last_sent_alert = time.time()
    return keybase_done or telegram_done


def check_issues():
    for key in list(issues.keys()):
        issue = issues[key]
        if issue['resolved']:
            res = alert(issue)
            if res:
                del issues[key]
            continue

        if issue['last_alert'] == 0:
            res = alert(issue)
            if res:
                issue['last_alert'] = time.time()
                issue['alert_number'] += 1
            continue

        next_interval = min(config.MIN_MSG_INTERVAL * 2 **
                            (issue['alert_number'] - 1), config.MAX_MSG_INTERVAL)
        next_alert = issue['last_alert'] + next_interval
        if next_alert <= time.time():
            res = alert(issue)
            if res:
                issue['last_alert'] = time.time()
                issue['alert_number'] += 1
    if time.time() - last_sent_alert > 24 * 60 * 60 and len(issues) == 0:
        res = alert({
            'resolved': True,
            'message': "There wasn't any issue in the past 24 hours"
        })


def issue_hash(service, issue_name):
    message = (service + issue_name).encode('ascii')
    h = base64.b64encode(sha256(message).digest()).decode('ascii')
    return h.replace('/', '_').replace('+', '-').replace('=', '')


def get_eidi_balance(addr):
    payload = json.dumps({
        'jsonrpc': '2.0',
        'method': 'eth_getBalance',
        'params': [addr, 'latest'],
        'id': 1
    })
    headers = {'content-type': 'application/json', 'cache-control': 'no-cache'}
    r = requests.request('POST', config.IDCHAIN_RPC_URLS[0],
                         data=payload, headers=headers)
    return int(r.json()['result'], 0) / 10**18


def check_sealers_activity():
    payload = json.dumps({
        'jsonrpc': '2.0',
        'method': 'clique_status',
        'params': [],
        'id': 1
    })
    headers = {'content-type': 'application/json', 'cache-control': 'no-cache'}
    r = requests.request('POST', config.IDCHAIN_RPC_URLS[0],
                         data=payload, headers=headers)
    status = r.json()['result']
    num_blocks = status['numBlocks']
    sealers_count = len(status['sealerActivity'])
    for sealer, sealed_block in status['sealerActivity'].items():
        key = issue_hash(sealer, 'not sealing')
        if sealed_block <= max(0, (num_blocks / sealers_count - config.SEALING_BORDER)):
            if key not in issues:
                issues[key] = {
                    'resolved': False,
                    'message': f'IDChain node {sealer} is not sealing blocks!',
                    'started_at': int(time.time()),
                    'last_alert': 0,
                    'alert_number': 0
                }
        else:
            if key in issues:
                issues[key]['resolved'] = True
                issues[key]['message'] = f'IDChain node {sealer} sealing issue is resolved.'


def check_idchain_lock():
    payload = json.dumps({
        'jsonrpc': '2.0',
        'method': 'eth_getBlockByNumber',
        'params': ['latest', False],
        'id': 1
    })
    headers = {'content-type': 'application/json', 'cache-control': 'no-cache'}
    r = requests.request('POST', config.IDCHAIN_RPC_URLS[0],
                         data=payload, headers=headers)
    block = r.json()['result']
    key = issue_hash('blockchain', 'idchain locked')
    if time.time() - int(block['timestamp'], 16) > config.DEADLOCK_BORDER:
        if key not in issues:
            issues[key] = {
                'resolved': False,
                'message': 'IDChain locked!!!',
                'started_at': int(time.time()),
                'last_alert': 0,
                'alert_number': 0
            }
    else:
        if key in issues:
            issues[key]['resolved'] = True
            issues[key]['message'] = 'IDChain lock issue is resolved.'


def check_distributor_balance():
    key = issue_hash(config.DISTRIBUTION_ETH_ADDRESS, 'eidi balance')
    balance = get_eidi_balance(config.DISTRIBUTION_ETH_ADDRESS)
    if balance < config.DISTRIBUTION_BALANCE_BORDER:
        if key not in issues:
            issues[key] = {
                'resolved': False,
                'message': f'Distribution contract ({config.DISTRIBUTION_ETH_ADDRESS}) does not have enough Eidi!',
                'started_at': int(time.time()),
                'last_alert': 0,
                'alert_number': 0
            }
    else:
        if key in issues:
            issues[key]['resolved'] = True
            issues[key]['message'] = 'Distribution contract Eidi balance issue is resolved.'


def check_relayer_balance():
    key = issue_hash(config.RELAYER_ETH_ADDRESS, 'eidi balance')
    balance = get_eidi_balance(config.RELAYER_ETH_ADDRESS)
    if balance < config.RELAYER_BALANCE_BORDER:
        if key not in issues:
            issues[key] = {
                'resolved': False,
                'message': f'Relayer ({config.RELAYER_ETH_ADDRESS}) does not have enough Eidi!',
                'started_at': int(time.time()),
                'last_alert': 0,
                'alert_number': 0
            }
    else:
        if key in issues:
            issues[key]['resolved'] = True
            issues[key]['message'] = 'Relayer service Eidi balance issue is resolved.'


def check_idchain_endpoints():
    # check rpc endpoints
    for endpoint in config.IDCHAIN_RPC_URLS:
        key = issue_hash(endpoint, 'idchain rpc endpoint')
        payload = json.dumps({
            'jsonrpc': '2.0',
            'method': 'eth_blockNumber',
            'params': [],
            'id': 1
        })
        headers = {'content-type': 'application/json',
                   'cache-control': 'no-cache'}
        try:
            resp = requests.request(
                'POST', endpoint, data=payload, headers=headers)
            if resp and resp.status_code == 200:
                if key in issues:
                    issues[key]['resolved'] = True
                    issues[key]['message'] = f'IDChain RPC endpoint ({endpoint}) issue is resolved.'
            else:
                raise Exception('connection error')
        except:
            if key not in issues:
                issues[key] = {
                    'resolved': False,
                    'message': f'IDChain RPC endpoint ({endpoint}) is not responding!',
                    'started_at': int(time.time()),
                    'last_alert': 0,
                    'alert_number': 0
                }

    # check ws endpoints
    for endpoint in config.IDCHAIN_WS_URLS:
        key = issue_hash(endpoint, 'idchain ws endpoint')
        try:
            ws = websocket.WebSocket()
            ws.connect(endpoint)
            if ws.connected:
                if key in issues:
                    issues[key]['resolved'] = True
                    issues[key]['message'] = f'IDChain WS endpoint ({endpoint}) issue is resolved.'
            else:
                raise Exception('connection error')
        except Exception as e:
            print(e)
            if key not in issues:
                issues[key] = {
                    'resolved': False,
                    'message': f'IDChain WS endpoint ({endpoint}) is not responding!',
                    'started_at': int(time.time()),
                    'last_alert': 0,
                    'alert_number': 0
                }


def check_idchain_explorer_service():
    key = issue_hash(config.IDCHAIN_EXPLORER_SERVICE,
                     'idchain explorer service')
    r = requests.get(config.IDCHAIN_EXPLORER_SERVICE)
    if not r or r.status_code != 200:
        if key not in issues:
            issues[key] = {
                'resolved': False,
                'message': f'IDChain explorer service ({config.IDCHAIN_EXPLORER_SERVICE}) is not responding!',
                'started_at': int(time.time()),
                'last_alert': 0,
                'alert_number': 0
            }
    else:
        if key in issues:
            issues[key]['resolved'] = True
            issues[key]['message'] = 'IDChain explorer service issue is resolved.'


def check_idchain_aragon_service():
    key = issue_hash(config.IDCHAIN_ARAGON_SERVICE, 'idchain aragon service')
    r = requests.get(config.IDCHAIN_ARAGON_SERVICE)
    if not r or r.status_code != 200:
        if key not in issues:
            issues[key] = {
                'resolved': False,
                'message': f'IDChain aragon service ({config.IDCHAIN_ARAGON_SERVICE}) is not responding!',
                'started_at': int(time.time()),
                'last_alert': 0,
                'alert_number': 0
            }
    else:
        if key in issues:
            issues[key]['resolved'] = True
            issues[key]['message'] = 'IDChain aragon service issue is resolved.'


def check_relayer_service():
    key = issue_hash(config.EIDI_BEGIN_PAGE, 'eidi begin service')
    r = requests.get(config.EIDI_BEGIN_PAGE)
    if not r or r.status_code != 200:
        if key not in issues:
            issues[key] = {
                'resolved': False,
                'message': f'IDChain begin page ({config.EIDI_BEGIN_PAGE}) is not responding!',
                'started_at': int(time.time()),
                'last_alert': 0,
                'alert_number': 0
            }
    else:
        if key in issues:
            issues[key]['resolved'] = True
            issues[key]['message'] = 'IDChain begin page issue is resolved.'

    key = issue_hash(config.EIDI_CLAIM_SERVICE, 'idchain relayer service')
    payload = json.dumps({
        "addr": "0x79af508c9698076bc1c2dfa224f7829e9768b11e"
    })
    headers = {'content-type': 'application/json', 'cache-control': 'no-cache'}
    r = requests.request('POST', config.EIDI_CLAIM_SERVICE,
                         data=payload, headers=headers)
    if not r or r.status_code != 200:
        if key not in issues:
            issues[key] = {
                'resolved': False,
                'message': f'IDChain relayer service ({config.EIDI_CLAIM_SERVICE}) is not responding!',
                'started_at': int(time.time()),
                'last_alert': 0,
                'alert_number': 0
            }
    else:
        if key in issues:
            issues[key]['resolved'] = True
            issues[key]['message'] = 'IDChain relayer service issue is resolved.'


def monitor_service():
    while True:
        try:
            check_sealers_activity()
        except Exception as e:
            print('Error check_sealers_activity', e)

        try:
            check_idchain_lock()
        except Exception as e:
            print('Error check_idchain_lock', e)

        try:
            check_distributor_balance()
        except Exception as e:
            print('Error check_distributor_balance', e)

        try:
            check_relayer_service()
        except Exception as e:
            print('Error check_relayer_service', e)

        try:
            check_relayer_balance()
        except Exception as e:
            print('Error check_relayer_balance', e)

        try:
            check_idchain_endpoints()
        except Exception as e:
            print('Error check_idchain_endpoints', e)

        try:
            check_idchain_explorer_service()
        except Exception as e:
            print('Error check_idchain_explorer_service', e)

        try:
            check_idchain_aragon_service()
        except Exception as e:
            print('Error check_idchain_aragon_service', e)

        time.sleep(config.CHECK_INTERVAL)


def alert_service():
    while True:
        check_issues()
        time.sleep(config.CHECK_INTERVAL)


if __name__ == '__main__':
    print('START')
    service1 = threading.Thread(target=monitor_service)
    service1.start()
    alert_service()
