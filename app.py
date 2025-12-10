from flask import Flask, request, jsonify, render_template, Response
import threading, time, json, hashlib, base64, datetime, queue
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.exceptions import InvalidSignature

# Configuration
DIFFICULTY = 3  # leading hex zeros required
START_REWARD = 50.0
COIN_CAP = 21_000_000.0

app = Flask(__name__, static_folder='static', template_folder='templates')

# In-memory blockchain state
blockchain_lock = threading.Lock()
chain = []  # list of blocks
pending_txs = []  # list of transactions (not yet mined)

# SSE clients: a list of queues (one per connected client)
sse_clients = []

# Utility functions
def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def serialize_block_for_hash(block):
    # deterministic representation for hashing (exclude 'hash' field)
    b = {
        'index': block['index'],
        'timestamp': block['timestamp'],
        'nonce': block['nonce'],
        'prev_hash': block['prev_hash'],
        'transactions': block['transactions'],
        'miner': block.get('miner', None)
    }
    return json.dumps(b, sort_keys=True, separators=(',', ':')).encode()

def compute_block_hash(block):
    return sha256_hex(serialize_block_for_hash(block))

def is_valid_chain(c):
    # Basic validation: chain continuity and block hashes & PoW
    for i in range(1, len(c)):
        prev = c[i-1]
        blk = c[i]
        if blk['prev_hash'] != prev['hash']:
            return False
        # validate hash
        if compute_block_hash(blk) != blk['hash']:
            return False
        # validate PoW
        if not blk['hash'].startswith('0' * DIFFICULTY):
            return False
        # (signature checks for transactions are done when tx accepted)
    return True

def total_issued(chain_obj):
    total = 0.0
    for blk in chain_obj:
        for tx in blk.get('transactions', []):
            if tx.get('from') == 'SYSTEM':
                total += float(tx.get('amount', 0))
    return total

def current_reward(chain_obj):
    issued = total_issued(chain_obj)
    # Determine halving count k such that for k>=0:
    # next halving threshold at COIN_CAP*(1 - 1/2^(k+1))
    k = 0
    while True:
        threshold = COIN_CAP * (1.0 - 1.0 / (2 ** (k + 1)))
        if issued >= threshold:
            k += 1
        else:
            break
    reward = START_REWARD / (2 ** k)
    # enforce coin cap
    if issued >= COIN_CAP:
        return 0.0
    if issued + reward > COIN_CAP:
        return max(0.0, COIN_CAP - issued)
    return reward

def verify_signature(spki_b64, signature_b64, message_bytes):
    try:
        spki_der = base64.b64decode(spki_b64)
        pubkey = serialization.load_der_public_key(spki_der)
        signature = base64.b64decode(signature_b64)
        pubkey.verify(signature, message_bytes, ec.ECDSA(hashes.SHA256()))
        return True
    except (ValueError, InvalidSignature, Exception):
        return False

def broadcast_event(event_name, data):
    payload = f"event: {event_name}\n" + f"data: {json.dumps(data)}\n\n"
    # push to client queues
    remove = []
    for q in sse_clients:
        try:
            q.put(payload, block=False)
        except Exception:
            remove.append(q)
    for q in remove:
        try:
            sse_clients.remove(q)
        except ValueError:
            pass

# Initialize genesis block
with blockchain_lock:
    genesis = {
        'index': 0,
        'timestamp': int(time.time()),
        'nonce': 0,
        'prev_hash': '0' * 64,
        'transactions': [],
        'miner': None
    }
    genesis['hash'] = compute_block_hash(genesis)
    chain.append(genesis)

# Flask routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_chain', methods=['GET'])
def get_chain():
    with blockchain_lock:
        return jsonify({'chain': chain, 'pending_txs': pending_txs})

@app.route('/submit_chain', methods=['POST'])
def submit_chain():
    payload = request.get_json()
    incoming = payload.get('chain')
    if not isinstance(incoming, list):
        return jsonify({'error': 'invalid chain format'}), 400
    with blockchain_lock:
        if len(incoming) > len(chain) and is_valid_chain(incoming):
            global chain
            chain = incoming
            # When chain replaced, clear pending txs that are included
            pending_txs.clear()
            broadcast_event('chain_replaced', {'chain_length': len(chain)})
            return jsonify({'status': 'replaced', 'length': len(chain)})
        else:
            return jsonify({'status': 'rejected', 'reason': 'incoming not longer or invalid', 'current_length': len(chain)})

@app.route('/submit_tx', methods=['POST'])
def submit_tx():
    tx = request.get_json()
    required = ['from', 'to', 'amount', 'signature', 'pubkey_spki']
    if not all(k in tx for k in required):
        return jsonify({'error': 'missing tx fields'}), 400
    # Validate signature
    message = json.dumps({'from': tx['from'], 'to': tx['to'], 'amount': float(tx['amount'])}, sort_keys=True, separators=(',', ':')).encode()
    if not verify_signature(tx['pubkey_spki'], tx['signature'], message):
        return jsonify({'error': 'invalid signature'}), 400
    # Verify that pubkey corresponds to 'from' (we expect 'from' to be base64 of pk or hex)
    # For simplicity, we use pubkey_spki base64 as identity on server. Client should set tx['from'] to that same string.
    if tx['from'] != tx['pubkey_spki']:
        return jsonify({'error': 'from does not match pubkey'}), 400
    # Check balance
    with blockchain_lock:
        bal = calculate_balance_internal(tx['from'])
        if float(tx['amount']) > bal:
            return jsonify({'error': 'insufficient funds', 'balance': bal}), 400
        # append to pending
        pending_txs.append({
            'from': tx['from'],
            'to': tx['to'],
            'amount': float(tx['amount']),
            'signature': tx['signature'],
            'pubkey_spki': tx['pubkey_spki']
        })
    broadcast_event('new_tx', {'tx': pending_txs[-1]})
    return jsonify({'status': 'accepted'})

@app.route('/balance', methods=['GET'])
def balance():
    pub = request.args.get('pub')
    if not pub:
        return jsonify({'error': 'no pub provided'}), 400
    with blockchain_lock:
        bal = calculate_balance_internal(pub)
    return jsonify({'balance': bal})

def calculate_balance_internal(pub_spki_b64):
    bal = 0.0
    # scan blocks
    for blk in chain:
        for tx in blk.get('transactions', []):
            if tx.get('to') == pub_spki_b64:
                bal += float(tx.get('amount', 0))
            if tx.get('from') == pub_spki_b64:
                bal -= float(tx.get('amount', 0))
    # pending txs: subtract outgoing pending
    for tx in pending_txs:
        if tx.get('from') == pub_spki_b64:
            bal -= float(tx.get('amount', 0))
    return bal

@app.route('/mine', methods=['POST'])
def mine():
    data = request.get_json()
    miner_pub = data.get('miner_pub')
    if not miner_pub:
        return jsonify({'error': 'miner_pub required'}), 400
    with blockchain_lock:
        reward = current_reward(chain)
        # create coinbase tx
        coinbase = {'from': 'SYSTEM', 'to': miner_pub, 'amount': float(reward)}
        # bundle transactions (copy)
        txs = [coinbase] + list(pending_txs)
        # build block
        new_block = {
            'index': len(chain),
            'timestamp': int(time.time()),
            'nonce': 0,
            'prev_hash': chain[-1]['hash'],
            'transactions': txs,
            'miner': miner_pub
        }
        # PoW mining
        target_prefix = '0' * DIFFICULTY
        nonce = 0
        found = False
        while True:
            new_block['nonce'] = nonce
            h = compute_block_hash(new_block)
            if h.startswith(target_prefix):
                new_block['hash'] = h
                found = True
                break
            nonce += 1
            # safety: avoid infinite loop in extremely constrained environments
            # no sleep here to speed up mining; Render CPU limits can slow it.
        if not found:
            return jsonify({'error': 'mining failed'}), 500
        # append
        chain.append(new_block)
        # clear pending that were included (we included all)
        pending_txs.clear()
        broadcast_event('mined', {'block_index': new_block['index'], 'hash': new_block['hash'], 'miner': miner_pub, 'reward': reward})
        return jsonify({'status': 'mined', 'block': new_block, 'reward': reward})

@app.route('/events')
def events():
    def gen(q):
        try:
            while True:
                msg = q.get()
                yield msg
        except GeneratorExit:
            pass

    q = queue.Queue()
    sse_clients.append(q)
    return Response(gen(q), mimetype='text/event-stream')

if __name__ == '__main__':
    # For Render, bind to 0.0.0.0 and default port
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
