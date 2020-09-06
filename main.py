import json
import time
import asyncio
import requests
from pykeybasebot import Bot
import pykeybasebot.types.chat1 as chat1
from config import *

sent = {}
bot = Bot(username=KEYBASE_BOT_USERNAME, paperkey=KEYBASE_BOT_KEY, handler=None)
def alert(msg):
    if msg in sent and time.time() - sent[msg] < SENT_TIMEOUT:
        return
    print(time.strftime("%a, %d %b %Y %H:%M:%S", time.gmtime()), msg)
    sent[msg] = int(time.time())
    channel = chat1.ChatChannel(**KEYBASE_BOT_CHANNEL)
    asyncio.run(bot.chat.send(channel, msg))

def check():
    payload = json.dumps({"jsonrpc": "2.0", "method": "clique_status", "params": [], "id": 1})
    headers = {'content-type': "application/json", 'cache-control': "no-cache"}
    r = requests.request("POST", IDCHAIN_RPC_URL, data=payload, headers=headers)
    status = r.json()['result']
    numBlocks = status['numBlocks']
    sealersCount = len(status['sealerActivity'])
    for sealer, sealedBlock in status['sealerActivity'].items():
        if sealedBlock < (numBlocks/sealersCount - SEALING_BORDER):
            alert(f'IDChain node {sealer}  is not sealing blocks!')

if __name__ == '__main__':
    while True:
        try:
            check()
            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt as e:
            raise
        except Exception as e:
            print(e)
