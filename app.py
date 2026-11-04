import os
import re
import time
import imaplib
import email
import datetime
import random
import string
import base64
import sqlite3
import hashlib
import secrets
from io import BytesIO
from email.header import decode_header
from email.utils import parsedate_to_datetime
from flask import Flask, request, jsonify, render_template, session, g
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import qrcode

# ---------------- CONFIG ----------------
app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True

DB_PATH = os.environ.get("DB_PATH", "fampay.db")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# ---------------- DATABASE ----------------
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name TEXT DEFAULT '',
            upi_id TEXT DEFAULT '',
            mobile TEXT DEFAULT '',
            email TEXT DEFAULT '',
            app_password TEXT DEFAULT '',
            total_earning REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT UNIQUE NOT NULL,
            username TEXT NOT NULL,
            expiry TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS qr_sessions (
            order_id TEXT PRIMARY KEY,
            api_key TEXT NOT NULL,
            username TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT DEFAULT 'PENDING',
            txn_id TEXT DEFAULT '',
            utr TEXT DEFAULT '',
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            order_id TEXT,
            amount REAL NOT NULL,
            txn_id TEXT,
            utr TEXT,
            paid_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        # demo user
        row = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()
        if not row:
            db.execute(
                "INSERT INTO users (username, password_hash, full_name, upi_id, email) VALUES (?,?,?,?,?)",
                ("admin", generate_password_hash("admin123"), "Riju Mondal", "riju@fam", "")
            )
        db.commit()

init_db()

# ---------------- HELPERS ----------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user' not in session:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper

def generate_api_key():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=16))

def create_qr_base64(data):
    qr = qrcode.QRCode(version=1, box_size=10, border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"

def clean_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try: body = part.get_payload(decode=True).decode(errors="ignore")
                except: pass
                break
    else:
        try: body = msg.get_payload(decode=True).decode(errors="ignore")
        except: pass
    return body

def check_fampay_email(username, expected_amount, minutes_window=15):
    """Sirf recent FamPay emails ko read karta hai (purane messages untouch)."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not user or not user['email'] or not user['app_password']:
        return False, None, "Email not configured"

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=15)
        mail.login(user['email'], user['app_password'].replace(" ", ""))
        mail.select("inbox")

        today = datetime.datetime.now().strftime("%d-%b-%Y")
        status, messages = mail.search(None, f'(SINCE "{today}" UNSEEN)')
        if status != "OK":
            return False, None, "Search failed"

        email_ids = messages[0].split()
        if not email_ids:
            mail.logout()
            return False, None, "No new emails"

        now_ist = datetime.datetime.now(IST)

        for e_id in email_ids[-8:]:  # Latest 8 messages check
            status, msg_data = mail.fetch(e_id, '(RFC822)')
            for rp in msg_data:
                if not isinstance(rp, tuple): continue
                msg = email.message_from_bytes(rp[1])
                subj_raw = msg.get('subject', '')
                try: subj = str(decode_header(subj_raw)[0][0])
                except: subj = subj_raw

                if "FamX" not in subj and "FamApp" not in subj and "Fam" not in subj:
                    continue

                # Time window check (India time)
                try:
                    dt = parsedate_to_datetime(msg.get('date'))
                    dt_ist = dt.astimezone(IST)
                    diff_min = (now_ist - dt_ist).total_seconds() / 60
                    if diff_min > minutes_window or diff_min < -3:
                        continue
                except:
                    continue

                body = clean_body(msg)
                amt_m = re.search(r'₹\s?([\d,]+\.?\d*)', body)
                if not amt_m: continue
                recv_amt = float(amt_m.group(1).replace(",", ""))
                if recv_amt < expected_amount: continue

                txn_m = re.search(r'Transaction ID\s*:\s*([A-Z0-9]+)', body)
                txn_id = txn_m.group(1) if txn_m else "UNKNOWN"
                utr_m = re.search(r'UTR\s*:\s*(\d+)', body)
                utr = utr_m.group(1) if utr_m else ""

                mail.store(e_id, '+FLAGS', '\\Seen')
                mail.logout()
                return True, {"txn_id": txn_id, "utr": utr, "amount": recv_amt}, "OK"

        mail.logout()
        return False, None, "No matching FamPay email"
    except Exception as e:
        return False, None, f"Email error: {str(e)}"

# ---------------- ROUTES ----------------

@app.route('/')
def home():
    return render_template("index.html")

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json or {}
    u = (data.get('username') or '').strip().lower()
    p = data.get('password') or ''
    if len(u) < 3 or len(p) < 4:
        return jsonify({"error": "Username min 3 chars, Password min 4 chars"}), 400
    db = get_db()
    try:
        db.execute("INSERT INTO users (username, password_hash) VALUES (?,?)",
                   (u, generate_password_hash(p)))
        db.commit()
        return jsonify({"status": "success", "message": "Account created"})
    except sqlite3.IntegrityError:
        return jsonify({"error": "Username already exists"}), 409

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    u = (data.get('username') or '').strip().lower()
    p = data.get('password') or ''
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
    if user and check_password_hash(user['password_hash'], p):
        session['user'] = u
        return jsonify({"status": "success", "user": u})
    return jsonify({"error": "Invalid credentials"}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"status": "success"})

@app.route('/api/me', methods=['GET'])
@login_required
def me():
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE username=?", (session['user'],)).fetchone()
    return jsonify({
        "username": u['username'],
        "full_name": u['full_name'],
        "upi_id": u['upi_id'],
        "mobile": u['mobile'],
        "email": u['email'],
        "has_app_password": bool(u['app_password']),
        "total_earning": round(u['total_earning'], 2)
    })

@app.route('/api/setup-account', methods=['POST'])
@login_required
def setup_account():
    d = request.json or {}
    db = get_db()
    db.execute("""UPDATE users SET full_name=?, upi_id=?, mobile=?, email=?, app_password=?
                  WHERE username=?""",
               (d.get('full_name',''), d.get('upi_id',''), d.get('mobile',''),
                d.get('email',''), d.get('app_password',''), session['user']))
    db.commit()
    return jsonify({"status": "success", "message": "Account updated"})

@app.route('/api/dashboard', methods=['GET'])
@login_required
def dashboard():
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE username=?", (session['user'],)).fetchone()
    txs = db.execute("""SELECT * FROM transactions WHERE username=?
                        ORDER BY id DESC LIMIT 20""", (session['user'],)).fetchall()
    return jsonify({
        "total_earning": round(u['total_earning'], 2),
        "upi_id": u['upi_id'],
        "transactions": [dict(t) for t in txs]
    })

@app.route('/api/generate-key', methods=['POST'])
@login_required
def generate_key():
    d = request.json or {}
    mode = d.get('mode', 'days')
    days = int(d.get('days', 30))
    if mode == 'lifetime':
        expiry = 'LIFETIME'
    else:
        expiry = (datetime.datetime.now() + datetime.timedelta(days=days)).isoformat()

    key = generate_api_key()
    db = get_db()
    db.execute("INSERT INTO api_keys (api_key, username, expiry) VALUES (?,?,?)",
               (key, session['user'], expiry))
    db.commit()
    base = request.host_url.rstrip('/')
    return jsonify({
        "status": "success",
        "api_key": key,
        "expiry": expiry,
        "api_url": f"{base}/v2/fam/{key}",
        "example": f"{base}/v2/fam/{key}&qr=100"
    })

@app.route('/api/list-keys', methods=['GET'])
@login_required
def list_keys():
    db = get_db()
    rows = db.execute("SELECT api_key, expiry, created_at FROM api_keys WHERE username=? ORDER BY id DESC",
                      (session['user'],)).fetchall()
    base = request.host_url.rstrip('/')
    return jsonify([{
        "api_key": r['api_key'],
        "expiry": r['expiry'],
        "created_at": r['created_at'],
        "url": f"{base}/v2/fam/{r['api_key']}"
    } for r in rows])

@app.route('/api/delete-key/<api_key>', methods=['DELETE'])
@login_required
def delete_key(api_key):
    db = get_db()
    db.execute("DELETE FROM api_keys WHERE api_key=? AND username=?", (api_key, session['user']))
    db.commit()
    return jsonify({"status": "success"})

@app.route('/api/check-payment/<order_id>', methods=['GET'])
def check_payment(order_id):
    db = get_db()
    s = db.execute("SELECT * FROM qr_sessions WHERE order_id=?", (order_id,)).fetchone()
    if not s:
        return jsonify({"status": "NOTFOUND"}), 404
    if s['status'] == 'PAID':
        return jsonify({"status": "PAID", "txn": {"txn_id": s['txn_id'], "utr": s['utr'], "amount": s['amount']}})
    if time.time() > s['expires_at']:
        db.execute("UPDATE qr_sessions SET status='EXPIRED' WHERE order_id=?", (order_id,))
        db.commit()
        return jsonify({"status": "EXPIRED"})

    ok, info, msg = check_fampay_email(s['username'], s['amount'], minutes_window=15)
    if ok:
        db.execute("UPDATE qr_sessions SET status='PAID', txn_id=?, utr=? WHERE order_id=?",
                   (info['txn_id'], info['utr'], order_id))
        db.execute("UPDATE users SET total_earning = total_earning + ? WHERE username=?",
                   (info['amount'], s['username']))
        db.execute("""INSERT INTO transactions (username, order_id, amount, txn_id, utr)
                      VALUES (?,?,?,?,?)""",
                   (s['username'], order_id, info['amount'], info['txn_id'], info['utr']))
        db.commit()
        return jsonify({"status": "PAID", "txn": info})
    return jsonify({"status": "PENDING"})

# ---------------- PUBLIC API ENDPOINT ----------------

@app.route('/v2/fam/<api_key>')
def fam_qr(api_key):
    # Support both '&qr=' and '?qr='
    raw = request.full_path.split('/v2/fam/')[1] if '/v2/fam/' in request.full_path else ''
    amount_str = None
    if '&qr=' in raw: amount_str = raw.split('&qr=')[1].split('&')[0].split('?')[0]
    else: amount_str = request.args.get('qr')

    if not amount_str:
        return _error_page("Missing &qr=AMOUNT parameter")
    try:
        amount = float(amount_str)
        if amount <= 0: raise ValueError
    except:
        return _error_page("Invalid amount")

    db = get_db()
    key_row = db.execute("SELECT * FROM api_keys WHERE api_key=?", (api_key,)).fetchone()
    if not key_row:
        return _error_page("Invalid API Key")

    if key_row['expiry'] != 'LIFETIME':
        try:
            exp = datetime.datetime.fromisoformat(key_row['expiry'])
            if datetime.datetime.now() > exp:
                return _error_page("API Key Expired")
        except: pass

    user = db.execute("SELECT * FROM users WHERE username=?", (key_row['username'],)).fetchone()
    if not user or not user['upi_id']:
        return _error_page("UPI ID not configured")

    upi_string = f"upi://pay?pa={user['upi_id']}&pn={user['full_name']}&am={amount}&cu=INR"
    qr_b64 = create_qr_base64(upi_string)

    order_id = "ORD" + str(int(time.time())) + str(random.randint(100,999))
    now = time.time()
    db.execute("""INSERT INTO qr_sessions (order_id, api_key, username, amount, created_at, expires_at)
                  VALUES (?,?,?,?,?,?)""",
               (order_id, api_key, key_row['username'], amount, now, now + 900))
    db.commit()

    return render_template("qr.html",
        amount=amount, qr_b64=qr_b64, order_id=order_id,
        upi_id=user['upi_id'], full_name=user['full_name'])

def _error_page(msg):
    return f"""<!DOCTYPE html><html><body style="font-family:Arial;background:#0b0b14;color:#fff;
    display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
    <div style="text-align:center;padding:30px;background:#14142b;border-radius:16px;border:1px solid #ff5757">
    <h2 style="color:#ff5757">❌ {msg}</h2></div></body></html>""", 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
