# app.py
import json
import time
import threading
import queue
from hashlib import sha256
from flask import Flask, request, jsonify, render_template, Response, stream_with_context

app = Flask(__name__)

# ==============
# Blockchain model (simple, in-memory)
# ==============
MAX_SUPPLY = 21_000_000
BASE_REWARD = 50
DIFFICULTY = 3  # default

def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def hash_block(block_dict):
    # deterministic serialization
    block_string = json.dumps(block_dict, sort_keys=True, separators=(',', ':'))
    return sha256(block_string.encode()).hexdigest()

class Blockchain:
    def __init__(self, difficulty=DIFFICULTY):
        self.chain = []
        self.pending = []
        self.difficulty = difficulty
        self.total_issued = 0  # total coins issued via coinbase txs
        # create genesis
        if not self.chain:
            genesis = {
                "index": 0,
                "timestamp": now_iso(),
                "nonce": 0,
                "previous_hash": "0" * 64,
                "transactions": [],
                "miner": None,
            }
            genesis["hash"] = hash_block(genesis)
            self.chain.append(genesis)

    def last_block(self):
        return self.chain[-1]

    def add_transaction(self, tx):
        # tx should be dict: {sender_pub, recipient_pub, amount, signature, timestamp}
        self.pending.append(tx)

    def compute_balance(self, pubkey):
        bal = 0.0
        for b in self.chain:
            for t in b.get("transactions", []):
                # coinbase tx has sender = "SYSTEM"
                sender = t.get("sender")
                recipient = t.get("recipient")
                amt = float(t.get("amount", 0))
                if recipient == pubkey:
                    bal += amt
                if sender == pubkey:
                    bal -= amt
        # include pending outgoing (not yet mined) as negative
        for t in self.pending:
            if t.get("sender") == pubkey:
                bal -= float(t.get("amount", 0))
        return bal

    def current_reward(self):
        # determine halving count:
        count = 0
        # thresholds: when total_issued >= MAX*(1 - 1/2^n) => reward has halved n times
        for n in range(1, 64):
            threshold = MAX_SUPPLY * (1 - 1 / (2 ** n))
            if self.total_issued >= threshold:
                count = n
            else:
                break
        reward = BASE_REWARD / (2 ** count)
        # avoid fractional tiny reward - return float for UI
        return float(reward)

    def mine_block(self, miner_pubkey):
        # coinbase tx:
        reward = self.current_reward()
        if self.total_issued + reward > MAX_SUPPLY:
            # adjust reward or disallow if max reached
            reward = max(0.0, MAX_SUPPLY - self.total_issued)
        coinbase = {
            "sender": "SYSTEM",
            "recipient": miner_pubkey,
            "amount": str(reward),
            "signature": None,
            "timestamp": now_iso()
        }

        transactions = [coinbase] + list(self.pending)
        index = len(self.chain)
        previous_hash = self.last_block()["hash"]
        nonce = 0
        target_prefix = "0" * self.difficulty

        while True:
            block = {
                "index": index,
                "timestamp": now_iso(),
                "nonce": nonce,
                "previous_hash": previous_hash,
                "transactions": transactions,
                "miner": miner_pubkey,
            }
            h = hash_block(block)
            if h.startswith(target_prefix):
                block["hash"] = h
                break
            nonce += 1

        # append
        self.chain.append(block)
        # update totals
        self.total_issued += reward
        # clear pending
        self.pending = []
        return block

    def to_dict(self):
        return {
            "chain": self.chain,
            "pending": self.pending,
            "difficulty": self.difficulty,
            "total_issued": self.total_issued,
            "max_supply": MAX_SUPPLY,
            "base_reward": BASE_REWARD
        }

    def replace_chain_if_longer(self, other_chain):
        if not other_chain:
            return False
        if len(other_chain) > len(self.chain):
            self.chain = other_chain
            # recalc total_issued from chain
            total = 0.0
            for b in self.chain:
                for t in b.get("transactions", []):
                    if t.get("sender") == "SYSTEM":
                        total += float(t.get("amount", 0))
            self.total_issued = total
            # reset pending (clients should resend pending txs if needed)
            self.pending = []
            return True
        return False

# instantiate
blockchain = Blockchain()

# SSE subscribers queue list
subscribers = []

sub_lock = threading.Lock()

def broadcast_chain_update():
    payload = json.dumps(blockchain.to_dict())
    with sub_lock:
        for q in list(subscribers):
            try:
                q.put(payload, block=False)
            except Exception:
                # drop broken subscribers
                try:
                    subscribers.remove(q)
                except ValueError:
                    pass

# ==============
# Flask routes
# ==============

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/chain", methods=["GET"])
def api_chain():
    return jsonify(blockchain.to_dict()), 200

@app.route("/api/submit_tx", methods=["POST"])
def api_submit_tx():
    data = request.json
    # expected fields: sender_pub, recipient_pub, amount, signature, timestamp
    sender = data.get("sender")
    recipient = data.get("recipient")
    amount = float(data.get("amount", 0))
    # signature and client-side signature verification assumed done client-side.
    # server restricts by balance check:
    if amount <= 0:
        return jsonify({"ok": False, "error": "invalid amount"}), 400
    bal = blockchain.compute_balance(sender)
    if bal < amount - 1e-9:
        return jsonify({"ok": False, "error": "insufficient balance", "balance": bal}), 400
    # accept as pending tx
    tx = {
        "sender": sender,
        "recipient": recipient,
        "amount": str(amount),
        "signature": data.get("signature"),
        "timestamp": data.get("timestamp") or now_iso()
    }
    blockchain.add_transaction(tx)
    # Optionally broadcast pending state (we broadcast chain updates only when mined)
    return jsonify({"ok": True, "pending_count": len(blockchain.pending)}), 200

@app.route("/api/sync_chain", methods=["POST"])
def api_sync_chain():
    data = request.json
    other_chain = data.get("chain")
    replaced = False
    if other_chain and isinstance(other_chain, list):
        replaced = blockchain.replace_chain_if_longer(other_chain)
        if replaced:
            # broadcast new chain to subscribers
            broadcast_chain_update()
            return jsonify({"ok": True, "replaced": True}), 200
    # if not replaced, server may send its chain back for clients to adopt if server is longer
    return jsonify({"ok": True, "replaced": False, "server_chain": blockchain.to_dict()}), 200

@app.route("/api/mine", methods=["POST"])
def api_mine():
    data = request.json or {}
    miner_pub = data.get("miner")
    if not miner_pub:
        return jsonify({"ok": False, "error": "miner public key required"}), 400
    # server will mine (PoW)
    block = blockchain.mine_block(miner_pub)
    # broadcast
    broadcast_chain_update()
    return jsonify({"ok": True, "block": block, "chain_length": len(blockchain.chain)}), 200

@app.route("/api/reset_pending", methods=["POST"])
def api_reset_pending():
    blockchain.pending = []
    return jsonify({"ok": True}), 200

@app.route("/stream")
def stream():
    def gen():
        q = queue.Queue()
        with sub_lock:
            subscribers.append(q)
        try:
            # initial send: current chain
            init = json.dumps(blockchain.to_dict())
            yield f"data: {init}\n\n"
            while True:
                payload = q.get()
                yield f"data: {payload}\n\n"
        except GeneratorExit:
            # client disconnected
            pass
        finally:
            with sub_lock:
                try:
                    subscribers.remove(q)
                except ValueError:
                    pass
    return Response(stream_with_context(gen()), mimetype="text/event-stream")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(5000))
