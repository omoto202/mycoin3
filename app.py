import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from flask import Flask, jsonify, request, render_template, Response
from uuid import uuid4
from ecdsa import VerifyingKey, SECP256k1, BadSignatureError
import queue

# --- 設定 ---
MINING_REWARD_INITIAL = 50
MAX_COIN_SUPPLY = 21000000
DIFFICULTY = 3

app = Flask(__name__)

# SSE用のキュー
message_queue = queue.Queue()

# --- ブロックチェーンクラス ---
class Blockchain:
    def __init__(self):
        self.chain = []
        self.current_transactions = []
        # ジェネシスブロック生成 (No.0, Nonce=0, Prev=0)
        self.new_block(previous_hash='0', proof=0)

    def new_block(self, proof, previous_hash=None):
        """
        ブロックチェーンに新しいブロックを作る
        """
        # 現在の総発行枚数を計算
        issued_amount = 0
        for b in self.chain:
            for tx in b['transactions']:
                if tx['sender'] == '0':
                    issued_amount += tx['amount']
        
        # 報酬の計算 (半減期ロジック)
        reward = MINING_REWARD_INITIAL
        current_threshold = MAX_COIN_SUPPLY / 2
        
        # 簡易的な減衰計算
        temp_issued = issued_amount
        while temp_issued >= current_threshold:
            reward /= 2
            current_threshold += (MAX_COIN_SUPPLY - current_threshold) / 2
            if reward < 0.00000001: break

        # 前のブロックのハッシュを決定
        if previous_hash:
            ph = previous_hash
        elif self.chain:
            ph = self.chain[-1]['hash']
        else:
            ph = '0'

        block = {
            'index': len(self.chain), # 0スタート
            'timestamp': datetime.now(timezone(timedelta(hours=9))).isoformat(), # JST
            'transactions': self.current_transactions,
            'proof': proof,
            'previous_hash': ph,
            'reward_at_block': reward
        }

        # 自身のハッシュを計算してブロックに含める
        block['hash'] = self.hash(block)

        self.current_transactions = []
        self.chain.append(block)
        return block

    def new_transaction(self, sender, recipient, amount, signature=None):
        """
        新しいトランザクションをリストに加える
        """
        transaction = {
            'sender': sender,
            'recipient': recipient,
            'amount': amount,
            'signature': signature,
            'timestamp': datetime.now(timezone(timedelta(hours=9))).isoformat()
        }
        self.current_transactions.append(transaction)
        # 次のブロックのインデックスを返す
        return len(self.chain)

    @staticmethod
    def hash(block):
        """
        ブロックのSHA-256ハッシュを作る
        注意: ブロック辞書の中に既に 'hash' キーがある場合は除外して計算する
        """
        block_copy = block.copy()
        if 'hash' in block_copy:
            del block_copy['hash']
            
        block_string = json.dumps(block_copy, sort_keys=True).encode()
        return hashlib.sha256(block_string).hexdigest()

    @property
    def last_block(self):
        return self.chain[-1]

    def proof_of_work(self, last_proof):
        """
        シンプルなPoW: hash(pp')の先頭が000...となるp'を探す
        """
        proof = 0
        while self.valid_proof(last_proof, proof) is False:
            proof += 1
        return proof

    @staticmethod
    def valid_proof(last_proof, proof):
        guess = f'{last_proof}{proof}'.encode()
        guess_hash = hashlib.sha256(guess).hexdigest()
        return guess_hash[:DIFFICULTY] == "0" * DIFFICULTY

    def valid_chain(self, chain):
        """
        チェーンの整合性を確認
        """
        last_block = chain[0]
        current_index = 1

        # ジェネシスブロックのチェック (簡易)
        if last_block['index'] != 0 or last_block['previous_hash'] != '0':
            return False

        while current_index < len(chain):
            block = chain[current_index]
            
            # ブロックの previous_hash が前のブロックの hash と一致するか
            if block['previous_hash'] != last_block['hash']:
                return False

            # 前のブロックのハッシュ値自体が正しいか再計算チェック（改竄検知）
            if self.hash(last_block) != last_block['hash']:
                return False

            # PoWのチェック
            if not self.valid_proof(last_block['proof'], block['proof']):
                return False

            last_block = block
            current_index += 1

        return True

    def calculate_balance(self, address):
        balance = 0
        for block in self.chain:
            for tx in block['transactions']:
                if tx['recipient'] == address:
                    balance += tx['amount']
                if tx['sender'] == address:
                    balance -= tx['amount']
        
        # 未承認トランザクションも考慮
        for tx in self.current_transactions:
            if tx['sender'] == address:
                balance -= tx['amount']
                
        return balance

# ブロックチェーンのインスタンス化
blockchain = Blockchain()

# --- ルート定義 ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/transactions/new', methods=['POST'])
def new_transaction():
    values = request.get_json()
    required = ['sender', 'recipient', 'amount', 'signature']
    if not all(k in values for k in required):
        return 'Missing values', 400

    if values['sender'] != '0':
        current_balance = blockchain.calculate_balance(values['sender'])
        if current_balance < float(values['amount']):
            return jsonify({'message': '残高不足です', 'status': 'fail'}), 400

    index = blockchain.new_transaction(values['sender'], values['recipient'], float(values['amount']), values['signature'])
    return jsonify({'message': f'トランザクションはブロック #{index} に追加されます', 'status': 'success'}), 201

@app.route('/mine', methods=['POST'])
def mine():
    values = request.get_json()
    miner_address = values.get('miner_address')

    if not miner_address:
        return 'Miner address missing', 400

    last_block = blockchain.last_block
    last_proof = last_block['proof']
    
    proof = blockchain.proof_of_work(last_proof)

    # 報酬計算（簡易）
    reward = MINING_REWARD_INITIAL
    # 現在の発行済量から計算
    issued = 0
    for b in blockchain.chain:
        for tx in b['transactions']:
            if tx['sender'] == '0':
                issued += tx['amount']
    
    current_threshold = MAX_COIN_SUPPLY / 2
    while issued >= current_threshold:
        reward /= 2
        current_threshold += (MAX_COIN_SUPPLY - current_threshold) / 2
        if reward < 0.00000001: break
    
    # 報酬トランザクション
    blockchain.new_transaction(
        sender="0",
        recipient=miner_address,
        amount=reward
    )

    # ブロック生成
    block = blockchain.new_block(proof)

    message_queue.put("new_block")

    return jsonify({'message': 'マイニング成功', 'block': block}), 200

@app.route('/chain', methods=['GET'])
def full_chain():
    response = {
        'chain': blockchain.chain,
        'length': len(blockchain.chain),
    }
    return jsonify(response), 200

@app.route('/nodes/resolve', methods=['POST'])
def consensus():
    values = request.get_json()
    client_chain = values.get('chain')

    if not client_chain:
        return jsonify({'message': 'No chain provided', 'chain': blockchain.chain}), 400

    if len(client_chain) > len(blockchain.chain):
        # クライアントチェーンを採用
        # 本来は検証が必要だがデモ用に受け入れる
        blockchain.chain = client_chain
        blockchain.current_transactions = [] # プールをリセット
        message = 'サーバーのチェーンが更新されました'
        new_chain = client_chain
    else:
        message = 'サーバーのチェーンが維持されました'
        new_chain = blockchain.chain

    return jsonify({'message': message, 'chain': new_chain}), 200

@app.route('/balance', methods=['POST'])
def get_balance():
    values = request.get_json()
    address = values.get('address')
    balance = blockchain.calculate_balance(address)
    return jsonify({'balance': balance}), 200

# --- SSE ---
def event_stream():
    while True:
        msg = message_queue.get()
        yield f"data: {msg}\n\n"

@app.route('/events')
def sse():
    return Response(event_stream(), mimetype="text/event-stream")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
