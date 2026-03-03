#!/usr/local/bin/python3.12
"""
Auto-redeem winning Polymarket positions.
Calls redeemPositions on the CTF contract on Polygon.

Usage:
  python3.12 redeem.py <condition_id>     # Redeem specific market
  python3.12 redeem.py --all              # Redeem all resolved winners
"""

import json, sys, os
from pathlib import Path
from web3 import Web3
from web3.constants import HASH_ZERO

CREDS_FILE = Path(__file__).parent.parent / ".polymarket-creds.json"
RPC_URL = "https://1rpc.io/matic"

# Polygon addresses
USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

REDEEM_ABI = json.loads("""[{"constant":false,"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"payable":false,"stateMutability":"nonpayable","type":"function"}]""")

def redeem(condition_id, private_key):
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    account = w3.eth.account.from_key(private_key)
    
    ctf = w3.eth.contract(
        address=w3.to_checksum_address(CTF),
        abi=REDEEM_ABI
    )
    
    # Build transaction
    txn = ctf.functions.redeemPositions(
        w3.to_checksum_address(USDC),
        HASH_ZERO,
        condition_id,
        [1, 2],  # Binary market index sets
    ).build_transaction({
        'from': account.address,
        'nonce': w3.eth.get_transaction_count(account.address),
        'gas': 300000,
        'gasPrice': w3.eth.gas_price,
        'chainId': 137,
    })
    
    # Sign and send
    signed = w3.eth.account.sign_transaction(txn, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  Redeem tx: {w3.to_hex(tx_hash)}")
    
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt['status'] == 1:
        print(f"  ✓ Redeemed! Gas used: {receipt['gasUsed']}")
        return True
    else:
        print(f"  ✗ Redeem failed")
        return False

def main():
    creds = json.loads(CREDS_FILE.read_text())
    pk = creds["privateKey"]
    
    if len(sys.argv) < 2:
        print("Usage: python3.12 redeem.py <condition_id> | --all")
        return
    
    if sys.argv[1] == "--all":
        # Redeem all from sniper ledger
        ledger_file = Path(__file__).parent / "ledgers" / "sniper.json"
        if not ledger_file.exists():
            print("No sniper ledger found")
            return
        ledger = json.loads(ledger_file.read_text())
        for trade in ledger["trades"]:
            if trade.get("outcome") == "WIN" and trade.get("condition_id") and not trade.get("redeemed"):
                print(f"Redeeming {trade['slug']} ({trade['side']})...")
                try:
                    ok = redeem(trade["condition_id"], pk)
                    if ok:
                        trade["redeemed"] = True
                except Exception as e:
                    print(f"  Error: {e}")
        ledger_file.write_text(json.dumps(ledger, indent=2))
    else:
        condition_id = sys.argv[1]
        if not condition_id.startswith("0x"):
            condition_id = "0x" + condition_id
        redeem(condition_id, pk)

if __name__ == "__main__":
    main()
