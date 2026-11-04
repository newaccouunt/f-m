import os, re, time, imaplib, email, datetime, random, string, base64, sqlite3, secrets
from io import BytesIO
from email.header import decode_header
from email.utils import parsedate_to_datetime
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, g
from werkzeug.security import generate_password_hash, check_password_hash
import qrcode

# ---------------- CONFIG ----------------
app = Flask(__name__, template_folder="templates")
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True
DB_PATH = os.environ.get("DB_PATH", "fampay.db")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
BASE_URL = os.environ.get("BASE_URL", "") 

# ---------------- DB ----------------
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db: db.close()

def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name TEXT DEFAULT '', upi_id TEXT DEFAULT '',
            mobile TEXT DEFAULT '', email TEXT DEFAULT '',
            app_password TEXT DEFAULT '', total_earning REAL DEFAULT 0,
            default_qr_minutes INTEGER DEFAULT 15,
            email_status TEXT DEFAULT 'unknown',
            email_last_check TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key TEXT UNIQUE NOT NULL, username TEXT NOT NULL,
            expiry TEXT NOT NULL, label TEXT DEFAULT '',
            total_requests INTEGER DEFAULT 0, total_qr INTEGER DEFAULT 0,
            total_received REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS qr_sessions (
            order_id TEXT PRIMARY KEY, api_key TEXT NOT NULL,
            username TEXT NOT NULL, amount REAL NOT NULL,
            status TEXT DEFAULT 'PENDING',
            txn_id TEXT DEFAULT '', utr TEXT DEFAULT '',
            paid_amount REAL DEFAULT 0,
            created_at REAL NOT NULL, expires_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL,
            order_id TEXT, amount REAL NOT NULL,
            txn_id TEXT, utr TEXT,
            paid_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        if not db.execute("SELECT id FROM users WHERE username='admin'").fetchone():
            db.execute("INSERT INTO users (username,password_hash,full_name,upi_id) VALUES (?,?,?,?)",
                       ("admin", generate_password_hash("admin123"), "Riju Mondal", "riju@fam"))
        db.commit()
init_db()

# ---------------- HELPERS ----------------
def login_required(f):
    @wraps(f)
    def wrapper(*a, **k):
        if 'user' not in session:
            return jsonify({"error":"Unauthorized"}), 401
        return f(*a, **k)
    return wrapper

def gen_key(): return ''.join(random.choices(string.ascii_uppercase+string.digits, k=16))

def qr_b64(data):
    q = qrcode.QRCode(version=1, box_size=10, border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(data); q.make(fit=True)
    img = q.make_image(fill_color="black", back_color="white")
    buf = BytesIO(); img.save(buf, format="PNG")
    return f"data:image/png;base64,{base64.b64encode(buf.getvalue()).decode()}"

def clean_body(msg):
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type()=="text/plain":
                try: return p.get_payload(decode=True).decode(errors="ignore")
                except: return ""
    else:
        try: return msg.get_payload(decode=True).decode(errors="ignore")
        except: return ""
    return ""

def read_fampay_email(username, expected_amount):
    """
    Ultra-fast: Only checks emails received TODAY (India time). 
    Never touches 4000+ old messages.
    """
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not u or not u['email'] or not u['app_password']:
        return False, None, "Email/App Password missing"
    
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=10)
        mail.login(u['email'], u['app_password'].replace(" ", ""))
        mail.select("inbox", readonly=False)

        # SIRF AAJ KE DIN KE EMAILS SEARCH KARO (India Time)
        today_date = datetime.datetime.now(IST).strftime("%d-%b-%Y")
        
        # Search only today's FamPay unread emails
        status, messages = mail.search(None, f'(SINCE "{today_date}" FROM "FamApp")')
        if status != "OK" or not messages[0]:
            mail.logout()
            return False, None, "No new FamPay email today"

        email_ids = messages[0].split()
        
        # Latest 5 emails check karo (reverse loop)
        for e_id in reversed(email_ids[-5:]):
            status, data = mail.fetch(e_id, '(RFC822)')
            for rp in data:
                if not isinstance(rp, tuple): continue
                msg = email.message_from_bytes(rp[1])
                
                # Check India Time
                try:
                    dt = parsedate_to_datetime(msg.get('date')).astimezone(IST)
                    now_ist = datetime.datetime.now(IST)
                    diff_min = (now_ist - dt).total_seconds() / 60
                    if diff_min > 60 or diff_min < -2: # 1 hour window
                        continue
                except: continue

                body = clean_body(msg)
                am = re.search(r'₹\s?([\d,]+\.?\d*)', body)
                if not am: continue
                
                recv = float(am.group(1).replace(",", ""))
                if recv < expected_amount - 0.01: continue

                txn_m = re.search(r'Transaction ID\s*:\s*([A-Z0-9]+)', body)
                txn_id = txn_m.group(1) if txn_m else "UNKNOWN"
                utr_m = re.search(r'UTR\s*:\s*(\d+)', body)
                utr = utr_m.group(1) if utr_m else ""

                mail.store(e_id, '+FLAGS', '\\Seen')
                mail.logout()
                return True, {"txn_id": txn_id, "utr": utr, "amount": recv}, "OK"
        
        mail.logout()
        return False, None, "No matching payment today"
    except Exception as e:
        return False, None, str(e)

def build_url(path):
    base = BASE_URL.rstrip('/') or request.host_url.rstrip('/')
    return base + path

# ---------------- ROUTES ----------------
@app.route('/')
def home(): return render_template("index.html")

@app.route('/api/register', methods=['POST'])
def register():
    d = request.json or {}
    u = (d.get('username') or '').strip().lower()
    p = d.get('password') or ''
    if len(u) < 3 or len(p) < 4: return jsonify({"error":"Min 3 chars for user, 4 for pass"}), 400
    db = get_db()
    try:
        db.execute("INSERT INTO users (username,password_hash) VALUES (?,?)", (u, generate_password_hash(p)))
        db.commit(); return jsonify({"status":"success"})
    except: return jsonify({"error":"Username exists"}), 409

@app.route('/api/login', methods=['POST'])
def login():
    d = request.json or {}
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE username=?", (d.get('username','').lower(),)).fetchone()
    if row and check_password_hash(row['password_hash'], d.get('password','')):
        session['user'] = row['username']
        return jsonify({"status":"success","user":row['username']})
    return jsonify({"error":"Invalid credentials"}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear(); return jsonify({"status":"success"})

@app.route('/api/me', methods=['GET'])
@login_required
def me():
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE username=?", (session['user'],)).fetchone()
    return jsonify({
        "username": u['username'], "full_name": u['full_name'], "upi_id": u['upi_id'],
        "mobile": u['mobile'], "email": u['email'], "has_app_password": bool(u['app_password']),
        "total_earning": round(u['total_earning'],2), "default_qr_minutes": u['default_qr_minutes'],
        "email_status": u['email_status'], "email_last_check": u['email_last_check'],
    })

@app.route('/api/setup-account', methods=['POST'])
@login_required
def setup_account():
    d = request.json or {}
    db = get_db()
    db.execute("""UPDATE users SET full_name=?, upi_id=?, mobile=?, email=?, app_password=?, default_qr_minutes=? 
                  WHERE username=?""",
               (d.get('full_name',''), d.get('upi_id',''), d.get('mobile',''),
                d.get('email',''), d.get('app_password',''), int(d.get('default_qr_minutes', 15)), session['user']))
    db.commit()
    return jsonify({"status":"success", "message": "Account details saved!"})

@app.route('/api/test-email', methods=['POST'])
@login_required
def test_email():
    d = request.json or {}
    email_addr = d.get('email') or ''
    app_pass = d.get('app_password') or ''
    if not email_addr or not app_pass:
        return jsonify({"ok": False, "error": "Email and App Password required"}), 400
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=12)
        mail.login(email_addr, app_pass.replace(" ", ""))
        mail.select("inbox", readonly=True)
        typ, msgs = mail.search(None, '(UNSEEN)')
        unseen = len(msgs[0].split()) if msgs and msgs[0] else 0
        mail.logout()
        db = get_db()
        db.execute("UPDATE users SET email_status='online', email_last_check=? WHERE username=?",
                   (datetime.datetime.now(IST).isoformat(), session['user'])); db.commit()
        return jsonify({"ok": True, "message": "✅ Gmail connected!", "unseen_emails": unseen})
    except Exception as e:
        db = get_db()
        db.execute("UPDATE users SET email_status='offline' WHERE username=?", (session['user'],)); db.commit()
        return jsonify({"ok": False, "error": f"❌ Failed: {str(e)}"}), 400

@app.route('/api/dashboard', methods=['GET'])
@login_required
def dashboard():
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE username=?", (session['user'],)).fetchone()
    keys = db.execute("SELECT * FROM api_keys WHERE username=?", (session['user'],)).fetchall()
    return jsonify({
        "total_earning": round(u['total_earning'],2), "upi_id": u['upi_id'],
        "email_status": u['email_status'], "email_last_check": u['email_last_check'],
        "total_api_keys": len(keys), "total_requests": sum(k['total_requests'] for k in keys),
        "total_qr_generated": sum(k['total_qr'] for k in keys),
        "total_received_via_api": round(sum(k['total_received'] for k in keys),2)
    })

@app.route('/api/generate-key', methods=['POST'])
@login_required
def gen_key_api():
    d = request.json or {}
    mode = d.get('mode', 'days'); days = int(d.get('days', 30) or 30); label = d.get('label', '')[:40]
    expiry = 'LIFETIME' if mode == 'lifetime' else (datetime.datetime.now()+datetime.timedelta(days=days)).isoformat()
    key = gen_key()
    db = get_db()
    db.execute("INSERT INTO api_keys (api_key,username,expiry,label) VALUES (?,?,?,?)", (key, session['user'], expiry, label))
    db.commit()
    return jsonify({"status":"success", "api_key": key, "expiry": expiry,
        "api_url": build_url(f"/v2/fam/{key}"), "example": build_url(f"/v2/fam/{key}&qr=100")})

@app.route('/api/list-keys', methods=['GET'])
@login_required
def list_keys():
    db = get_db()
    rows = db.execute("SELECT * FROM api_keys WHERE username=? ORDER BY id DESC", (session['user'],)).fetchall()
    return jsonify([{
        "api_key": r['api_key'], "expiry": r['expiry'], "label": r['label'],
        "total_requests": r['total_requests'], "total_qr": r['total_qr'], "total_received": round(r['total_received'],2),
        "url": build_url(f"/v2/fam/{r['api_key']}")
    } for r in rows])

@app.route('/api/delete-key/<api_key>', methods=['DELETE'])
@login_required
def del_key(api_key):
    db = get_db()
    db.execute("DELETE FROM api_keys WHERE api_key=? AND username=?", (api_key, session['user']))
    db.commit()
    return jsonify({"status":"success"})

# ---------------- PUBLIC API (Telegram/Bots) ----------------
def _key_status(key):
    db = get_db()
    row = db.execute("SELECT * FROM api_keys WHERE api_key=?", (key,)).fetchone()
    if not row: return None, "invalid"
    if row['expiry'] != 'LIFETIME':
        try:
            if datetime.datetime.now() > datetime.datetime.fromisoformat(row['expiry']): return row, "expired"
        except: pass
    return row, "active"

@app.route('/v2/fam/<api_key>')
def fam_qr(api_key):
    raw = request.full_path.split('/v2/fam/')[1] if '/v2/fam/' in request.full_path else ''
    amount_str = raw.split('&qr=')[1].split('&')[0].split('?')[0] if '&qr=' in raw else request.args.get('qr')
    if not amount_str: return "Missing &qr=AMOUNT", 400
    try: amount = float(amount_str)
    except: return "Invalid amount", 400

    key_row, status = _key_status(api_key)
    if status != "active": return f"API Key {status}", 403

    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username=?", (key_row['username'],)).fetchone()
    if not user or not user['upi_id']: return "UPI not set", 400

    db.execute("UPDATE api_keys SET total_requests=total_requests+1, total_qr=total_qr+1 WHERE api_key=?", (api_key,)); db.commit()

    minutes = int(request.args.get('minutes', user['default_qr_minutes'] or 15))
    upi = f"upi://pay?pa={user['upi_id']}&pn={user['full_name']}&am={amount}&cu=INR"
    order_id = "ORD" + str(int(time.time())) + str(random.randint(100,999))
    now = time.time()
    
    db.execute("INSERT INTO qr_sessions (order_id,api_key,username,amount,created_at,expires_at) VALUES (?,?,?,?,?,?)",
               (order_id, api_key, key_row['username'], amount, now, now + minutes*60))
    db.commit()
    
    return render_template("qr.html", amount=amount, qr_b64=qr_b64(upi),
                           order_id=order_id, upi_id=user['upi_id'], full_name=user['full_name'], minutes=minutes)

@app.route('/api/v2/check/<api_key>')
def api_check(api_key):
    key_row, status = _key_status(api_key)
    if status != "active": return jsonify({"error": status}), 403
    
    order_id = request.args.get('order_id')
    db = get_db()
    if order_id:
        s = db.execute("SELECT * FROM qr_sessions WHERE order_id=? AND api_key=?", (order_id, api_key)).fetchone()
    else:
        s = db.execute("SELECT * FROM qr_sessions WHERE api_key=? ORDER BY created_at DESC LIMIT 1", (api_key,)).fetchone()
    
    if not s: return jsonify({"status":"NOTFOUND"})
    if s['status'] == 'PAID':
        return jsonify({"status":"PAID", "order_id":s['order_id'], "amount":s['amount'], "txn_id":s['txn_id'], "utr":s['utr']})
    if time.time() > s['expires_at']:
        db.execute("UPDATE qr_sessions SET status='EXPIRED' WHERE order_id=?", (s['order_id'],)); db.commit()
        return jsonify({"status":"EXPIRED"})

    # SUPER FAST CHECK
    ok, info, msg = read_fampay_email(s['username'], s['amount'])
    if ok:
        db.execute("UPDATE qr_sessions SET status='PAID',txn_id=?,utr=?,paid_amount=? WHERE order_id=?",
                   (info['txn_id'], info['utr'], info['amount'], s['order_id']))
        db.execute("UPDATE users SET total_earning=total_earning+? WHERE username=?", (info['amount'], s['username']))
        db.execute("UPDATE api_keys SET total_received=total_received+? WHERE api_key=?", (info['amount'], api_key))
        db.execute("INSERT INTO transactions (username,order_id,amount,txn_id,utr) VALUES (?,?,?,?,?)",
                   (s['username'], s['order_id'], info['amount'], info['txn_id'], info['utr']))
        db.commit()
        return jsonify({"status":"PAID", "order_id":s['order_id'], "amount":info['amount'], "txn_id":info['txn_id'], "utr":info['utr']})
    
    return jsonify({"status":"PENDING", "order_id":s['order_id'], "seconds_left": int(max(0, s['expires_at']-time.time()))})

@app.route('/api/check-payment/<order_id>')
def check_payment(order_id):
    db = get_db()
    s = db.execute("SELECT * FROM qr_sessions WHERE order_id=?", (order_id,)).fetchone()
    if not s: return jsonify({"status":"NOTFOUND"}), 404
    if s['status'] == 'PAID': return jsonify({"status":"PAID","txn":{"txn_id":s['txn_id'],"utr":s['utr'],"amount":s['paid_amount'] or s['amount']}})
    if time.time() > s['expires_at']:
        db.execute("UPDATE qr_sessions SET status='EXPIRED' WHERE order_id=?", (order_id,)); db.commit()
        return jsonify({"status":"EXPIRED"})

    ok, info, msg = read_fampay_email(s['username'], s['amount'])
    if ok:
        db.execute("UPDATE qr_sessions SET status='PAID',txn_id=?,utr=?,paid_amount=? WHERE order_id=?",
                   (info['txn_id'], info['utr'], info['amount'], order_id))
        db.execute("UPDATE users SET total_earning=total_earning+? WHERE username=?", (info['amount'], s['username']))
        db.execute("UPDATE api_keys SET total_received=total_received+? WHERE api_key=?", (info['amount'], s['api_key']))
        db.execute("INSERT INTO transactions (username,order_id,amount,txn_id,utr) VALUES (?,?,?,?,?)",
                   (s['username'], order_id, info['amount'], info['txn_id'], info['utr']))
        db.commit()
        return jsonify({"status":"PAID","txn":info})
    return jsonify({"status":"PENDING"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
