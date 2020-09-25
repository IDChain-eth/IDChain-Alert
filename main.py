import json
import time
import asyncio
import requests
from pykeybasebot import Bot
import pykeybasebot.types.chat1 as chat1
from config import *

sent = {}
keybaseBot = None
if KEYBASE_BOT_KEY:
    keybaseBot = Bot(username=KEYBASE_BOT_USERNAME, paperkey=KEYBASE_BOT_KEY, handler=None)

def alert(msg):
    if msg in sent and time.time() - sent[msg] < SENT_TIMEOUT:
        return
    print(time.strftime("%a, %d %b %Y %H:%M:%S", time.gmtime()), msg)
    sent[msg] = int(time.time())
    if KEYBASE_BOT_KEY:
        try:
            channel = chat1.ChatChannel(**KEYBASE_BOT_CHANNEL)
            asyncio.run(keybaseBot.chat.send(channel, msg))
        except Exception as e:
            print('keybase error', e)
    if TELEGRAM_BOT_KEY:
        try:
            payload = json.dumps({"chat_id": TELEGRAM_BOT_CHANNEL, "text": msg})
            headers = {'content-type': "application/json", 'cache-control': "no-cache"}
            url = f'https://api.telegram.org/bot{TELEGRAM_BOT_KEY}/sendMessage'
            r = requests.post(url, data=payload, headers=headers)
        except Exception as e:
            print('telegram error', e)

def getIDChainBalance(addr):
    payload = json.dumps({"jsonrpc": "2.0", "method": "eth_getBalance", "params": [addr, 'latest'], "id": 1})
    headers = {'content-type': "application/json", 'cache-control': "no-cache"}
    r = requests.request("POST", IDCHAIN_RPC_URL, data=payload, headers=headers)
    return int(r.json()['result'], 0) / 10**18

def check():
    payload = json.dumps({"jsonrpc": "2.0", "method": "clique_status", "params": [], "id": 1})
    headers = {'content-type': "application/json", 'cache-control': "no-cache"}
    r = requests.request("POST", IDCHAIN_RPC_URL, data=payload, headers=headers)
    status = r.json()['result']
    numBlocks = status['numBlocks']
    sealersCount = len(status['sealerActivity'])
    for sealer, sealedBlock in status['sealerActivity'].items():
        if sealedBlock <= max(0, (numBlocks/sealersCount - SEALING_BORDER)):
            alert(f'IDChain node {sealer}  is not sealing blocks!')

    payload = json.dumps({"jsonrpc": "2.0", "method": "eth_getBlockByNumber", "params": ["latest", False], "id": 1})
    headers = {'content-type': "application/json", 'cache-control': "no-cache"}
    r = requests.request("POST", IDCHAIN_RPC_URL, data=payload, headers=headers)
    block = r.json()['result']
    if time.time() - int(block['timestamp'], 16) > DEADLOCK_BORDER:
        alert(f'IDChain locked!!!')

    balance = getIDChainBalance(RELAYER_ETH_ADDRESS)
    if balance < BALANCE_BORDER:
        alert('Relayer does not have enough Eidi balance to send required transactions!')

    r = requests.post('https://idchain.one/begin/api/claim', json={'addr': ''})
    if r.status_code != 200:
        alert('Relayer service is not responding!')

if __name__ == '__main__':
    while True:
        try:
            check()
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt as e:
            raise
        except Exception as e:
            print('error', e)
