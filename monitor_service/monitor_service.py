import hashlib
import logging
import time
import traceback
from threading import Thread
from typing import Any, Dict, Optional

import config
import redis
import requests
import websocket
from messages import ISSUE_MESSAGES

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# Initialize Redis
redis_client = redis.Redis(
    host=config.REDIS_HOST, port=config.REDIS_PORT, decode_responses=True
)


def insert_new_issue(issue_id: str, message: str) -> None:
    """Insert or update an issue in Redis using a hash structure."""
    issue = {
        "id": issue_id,
        "resolved": int(False),
        "message": message,
        "started_at": int(time.time()),
        "last_alert": 0,
        "alert_number": 0,
    }
    redis_client.hset(f"issue:{issue_id}", mapping=issue)


def is_issue_exists(issue_id: str) -> bool:
    """Check if an issue exists in Redis."""
    return redis_client.exists(f"issue:{issue_id}") > 0


def mark_issue_resolved(issue_id: str, message: str) -> None:
    """Mark an issue as resolved in Redis using a hash structure."""
    if redis_client.exists(f"issue:{issue_id}"):
        redis_client.hset(
            f"issue:{issue_id}", mapping={"resolved": int(True), "message": message}
        )


def update_health_status() -> None:
    """Update last check timestamp in Redis."""
    redis_client.set("health:monitor_service", int(time.time()))


def generate_issue_id(part1: str, part2: str) -> str:
    """Generate a unique hash for an issue."""
    message = f"{part1}|{part2}".encode("utf-8")
    return hashlib.sha256(message).hexdigest()


def send_rpc_request(
    url: str,
    method: str,
    params: list,
) -> Optional[Any]:
    """Send a RPC request"""
    request_data = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }
    headers = {"content-type": "application/json", "cache-control": "no-cache"}
    response = send_post_request(url, request_data, headers)
    try:
        return response.json().get("result", None)
    except ValueError as e:
        logging.error(f"Failed to parse JSON response from {url}: {e}")
        return None


def send_post_request(
    url: str,
    request_data: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Optional[requests.Response]:
    """Send an HTTP request"""
    try:
        response = requests.post(url, json=request_data, headers=headers)
        response.raise_for_status()
        return response
    except requests.exceptions.RequestException as e:
        logging.error(f"Request to {url} failed: {e}")
        return None


def get_eidi_balance(addr: str) -> float:
    """Get the Eidi balance of an Ethereum address."""
    balance = send_rpc_request(
        url=config.HTTPS_RPC_URLS[0], method="eth_getBalance", params=[addr, "latest"]
    )
    if not balance:
        return None
    try:
        return int(balance, 16) / 10**18
    except (ValueError, TypeError) as e:
        logging.error(f"Failed to parse balance for address {addr}: {e}. Balance value: {balance}")
        return None


def check_sealers_activity() -> bool:
    """Check the activity of sealing nodes on the IDChain network."""
    clique_status = send_rpc_request(
        url=config.HTTPS_RPC_URLS[0], method="clique_status", params=[]
    )
    if not clique_status:
        return False

    sealer_activity = clique_status.get("sealerActivity")
    num_blocks = clique_status.get("numBlocks")

    if not num_blocks or not isinstance(num_blocks, int):
        logging.error(f"Invalid numBlocks value: {num_blocks}")
        return False

    if not sealer_activity or not isinstance(sealer_activity, dict):
        logging.error(f"Invalid sealerActivity in clique_status: {sealer_activity}")
        return False

    sealers_count = len(sealer_activity)
    for sealer, sealed_block in sealer_activity.items():
        check_sealer_activity(
            sealer, sealed_block, num_blocks, sealers_count
        )
    return True


def check_sealer_activity(
    sealer: str, sealed_block: int, num_blocks: int, sealers_count: int
) -> None:
    """Check the activity of a sealing node on the IDChain network."""
    issue_id = generate_issue_id(sealer, "not sealing block")
    issue_exists = is_issue_exists(issue_id)
    if not issue_exists and sealed_block == 0:
        insert_new_issue(issue_id, ISSUE_MESSAGES["sealer_not_sealing"].format(sealer))
    elif issue_exists and sealed_block >= min(
        config.SEALING_BORDER, num_blocks / sealers_count
    ):
        mark_issue_resolved(
            issue_id, ISSUE_MESSAGES["sealer_sealing_resolved"].format(sealer)
        )


def check_idchain_lock() -> bool:
    """Check if the IDChain network is locked."""
    block = send_rpc_request(
        url=config.HTTPS_RPC_URLS[0],
        method="eth_getBlockByNumber",
        params=["latest", False],
    )
    if not block:
        return False

    issue_id = generate_issue_id("idchain", "locked")
    issue_exists = is_issue_exists(issue_id)
    try:
        block_timestamp = int(block["timestamp"], 16)
    except ValueError as e:
        logging.error(f"Invalid block timestamp: {e}")
        return False

    is_active = (time.time() - block_timestamp) < config.DEADLOCK_BORDER
    if not is_active and not issue_exists:
        insert_new_issue(
            issue_id,
            ISSUE_MESSAGES["idchain_locked"].format(config.IDCHAIN_EXPLORER_URL),
        )
    elif is_active and issue_exists:
        mark_issue_resolved(
            issue_id,
            ISSUE_MESSAGES["idchain_lock_resolved"].format(config.IDCHAIN_EXPLORER_URL),
        )
    return True


def check_distributor_balance() -> bool:
    """Check the balance of the distribution contract."""
    issue_id = generate_issue_id(config.DISTRIBUTION_ADDRESS, "eidi balance")
    issue_exists = is_issue_exists(issue_id)
    balance = get_eidi_balance(config.DISTRIBUTION_ADDRESS)
    if not balance:
        return False

    low_balance = balance < config.DISTRIBUTION_BALANCE_BORDER
    if low_balance and not issue_exists:
        insert_new_issue(
            issue_id,
            ISSUE_MESSAGES["distribution_low_balance"].format(
                config.DISTRIBUTION_ADDRESS
            ),
        )
    elif issue_exists and not low_balance:
        mark_issue_resolved(
            issue_id,
            ISSUE_MESSAGES["distribution_balance_resolved"].format(
                config.DISTRIBUTION_ADDRESS
            ),
        )
    return True


def check_relayer_balance() -> bool:
    """Check the balance of the relayer address."""
    issue_id = generate_issue_id(config.RELAYER_ADDRESS, "eidi balance")
    issue_exists = is_issue_exists(issue_id)
    balance = get_eidi_balance(config.RELAYER_ADDRESS)
    if not balance:
        return False

    low_balance = balance < config.RELAYER_BALANCE_BORDER
    if low_balance and not issue_exists:
        insert_new_issue(
            issue_id,
            ISSUE_MESSAGES["relayer_low_balance"].format(config.RELAYER_ADDRESS),
        )
    elif issue_exists and not low_balance:
        mark_issue_resolved(
            issue_id,
            ISSUE_MESSAGES["relayer_balance_resolved"].format(config.RELAYER_ADDRESS),
        )
    return True


def check_https_endpoints() -> bool:
    """Check the health of IDChain HTTPS endpoints."""
    for endpoint in config.HTTPS_RPC_URLS:
        issue_id = generate_issue_id(endpoint, "idchain https endpoint")
        issue_exists = is_issue_exists(issue_id)
        block_number_hex = send_rpc_request(
            url=endpoint,
            method="eth_blockNumber",
            params=[],
        )
        if not block_number_hex:
            return False

        succeeded = int(block_number_hex, 16) > 0 if block_number_hex else False
        if not succeeded and not issue_exists:
            insert_new_issue(
                issue_id,
                ISSUE_MESSAGES["https_endpoint_down"].format(endpoint),
            )
        elif succeeded and issue_exists:
            mark_issue_resolved(
                issue_id,
                ISSUE_MESSAGES["https_endpoint_resolved"].format(endpoint),
            )
    return True


def check_wss_endpoints() -> bool:
    """Check the health of IDChain WSS endpoints."""
    for endpoint in config.WSS_RPC_URLS:
        issue_id = generate_issue_id(endpoint, "idchain wss endpoint")
        issue_exists = is_issue_exists(issue_id)
        succeeded = False
        ws = websocket.WebSocket()
        try:
            ws.connect(endpoint)
            succeeded = ws.connected
        except Exception as e:
            logging.error(f"Failed to connect to WebSocket {endpoint}: {e}")
        finally:
            ws.close()

        if not succeeded and not issue_exists:
            insert_new_issue(
                issue_id,
                ISSUE_MESSAGES["wss_endpoint_down"].format(endpoint),
            )
        elif succeeded and issue_exists:
            mark_issue_resolved(
                issue_id,
                ISSUE_MESSAGES["wss_endpoint_resolved"].format(endpoint),
            )
    return True


def check_idchain_explorer_service() -> None:
    """Check the health of the IDChain explorer service."""
    issue_id = generate_issue_id(
        config.IDCHAIN_EXPLORER_URL, "idchain explorer service"
    )
    issue_exists = is_issue_exists(issue_id)
    try:
        response = requests.get(config.IDCHAIN_EXPLORER_URL)
        succeeded = response is not None and response.status_code == 200
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to check IDChain explorer service: {e}")
        succeeded = False
    if not succeeded and not issue_exists:
        insert_new_issue(
            issue_id,
            ISSUE_MESSAGES["explorer_service_down"].format(config.IDCHAIN_EXPLORER_URL),
        )
    elif succeeded and issue_exists:
        mark_issue_resolved(
            issue_id,
            ISSUE_MESSAGES["explorer_service_resolved"].format(
                config.IDCHAIN_EXPLORER_URL
            ),
        )


def check_idchain_aragon_service() -> None:
    """Check the health of the IDChain Aragon service."""
    issue_id = generate_issue_id(config.IDCHAIN_ARAGON_URL, "idchain aragon service")
    issue_exists = is_issue_exists(issue_id)
    try:
        response = requests.get(config.IDCHAIN_ARAGON_URL)
        succeeded = response is not None and response.status_code == 200
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to check IDChain Aragon service: {e}")
        succeeded = False
    if not succeeded and not issue_exists:
        insert_new_issue(
            issue_id,
            ISSUE_MESSAGES["aragon_service_down"].format(config.IDCHAIN_ARAGON_URL),
        )
    elif succeeded and issue_exists:
        mark_issue_resolved(
            issue_id,
            ISSUE_MESSAGES["aragon_service_resolved"].format(config.IDCHAIN_ARAGON_URL),
        )


def check_eidi_claim_page() -> None:
    """Check the health of the claim Eidi Page."""
    issue_id = generate_issue_id(config.EIDI_CLAIM_URL, "claim eidi page")
    issue_exists = is_issue_exists(issue_id)
    try:
        response = requests.get(config.EIDI_CLAIM_URL)
        succeeded = response is not None and response.status_code == 200
    except requests.exceptions.RequestException as e:
        logging.error(f"Failed to check Eidi claim page: {e}")
        succeeded = False
    if not succeeded and not issue_exists:
        insert_new_issue(
            issue_id,
            ISSUE_MESSAGES["claim_page_down"].format(config.EIDI_CLAIM_URL),
        )
    elif succeeded and issue_exists:
        mark_issue_resolved(
            issue_id,
            ISSUE_MESSAGES["claim_page_resolved"].format(config.EIDI_CLAIM_URL),
        )


def check_eidi_claim_api() -> None:
    """Check the health of the claim Eidi API."""
    issue_id = generate_issue_id(config.EIDI_CLAIM_API, "idchain relayer service")
    issue_exists = is_issue_exists(issue_id)
    request_data = {"addr": "0x79af508c9698076bc1c2dfa224f7829e9768b11e"}
    response = send_post_request(config.EIDI_CLAIM_API, request_data)
    succeeded = response is not None and response.status_code == 200
    if not succeeded and not issue_exists:
        insert_new_issue(
            issue_id,
            ISSUE_MESSAGES["claim_api_down"].format(config.EIDI_CLAIM_API),
        )
    elif succeeded and issue_exists:
        mark_issue_resolved(
            issue_id, ISSUE_MESSAGES["claim_api_resolved"].format(config.EIDI_CLAIM_API)
        )


def main() -> None:
    """Continuously monitor the health of IDChain services."""
    while True:
        try:
            check_https_endpoints()
            check_idchain_lock()
            check_wss_endpoints()
            check_sealers_activity()
            check_eidi_claim_page()
            check_eidi_claim_api()
            check_idchain_explorer_service()
            check_idchain_aragon_service()
            check_relayer_balance()
            check_distributor_balance()
            update_health_status()
        except Exception as e:
            logging.error(f"Error in monitor_service: {e}")
            logging.error(traceback.format_exc())

        time.sleep(config.CHECK_INTERVAL)


if __name__ == "__main__":
    logging.info("Starting Monitor Service...")
    monitor_thread = Thread(target=main)
    monitor_thread.start()
