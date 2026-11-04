import os, re, time, imaplib, email, datetime, random, string, base64, sqlite3, secrets
from io import BytesIO
from email.header import decode_header
from email.utils import parsedate_to_datetime
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, session, g
from werkzeug.security import generate_password_hash, check_password_hash
import qrcode

# ==========================================
# CONFIGURATION & DATABASE SETUP
# ==========================================
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True

DB_PATH = os.environ.get("DB_PATH", "fampay.db")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
BASE_URL = os.environ.get("BASE_URL", "")

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

# ==========================================
# HELPER FUNCTIONS
# ==========================================
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
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not u or not u['email'] or not u['app_password']:
        return False, None, "Email/App Password missing"
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=10)
        mail.login(u['email'], u['app_password'].replace(" ", ""))
        mail.select("inbox", readonly=False)
        
        today_date = datetime.datetime.now(IST).strftime("%d-%b-%Y")
        status, messages = mail.search(None, f'(SINCE "{today_date}" FROM "FamApp")')
        
        if status != "OK" or not messages[0]:
            mail.logout()
            return False, None, "No new FamPay email today"
            
        email_ids = messages[0].split()
        for e_id in reversed(email_ids[-5:]):
            status, data = mail.fetch(e_id, '(RFC822)')
            for rp in data:
                if not isinstance(rp, tuple): continue
                msg = email.message_from_bytes(rp[1])
                try:
                    dt = parsedate_to_datetime(msg.get('date')).astimezone(IST)
                    now_ist = datetime.datetime.now(IST)
                    diff_min = (now_ist - dt).total_seconds() / 60
                    if diff_min > 60 or diff_min < -2: continue
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

# ==========================================
# API ROUTES
# ==========================================
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
        typ, msgs = mail.search(None, 'ALL')
        total = len(msgs[0].split()) if msgs and msgs[0] else 0
        typ, msgs = mail.search(None, '(UNSEEN)')
        unseen = len(msgs[0].split()) if msgs and msgs[0] else 0
        mail.logout()
        db = get_db()
        db.execute("UPDATE users SET email_status='online', email_last_check=? WHERE username=?",
                   (datetime.datetime.now(IST).isoformat(), session['user'])); db.commit()
        return jsonify({"ok": True, "message": "✅ Gmail connected!", "total_emails": total, "unseen_emails": unseen})
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
    
    chart = []
    for i in range(6, -1, -1):
        day = (datetime.datetime.now(IST) - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        s = db.execute("SELECT SUM(amount) as s FROM transactions WHERE username=? AND DATE(paid_at)=?",
                       (session['user'], day)).fetchone()['s'] or 0
        chart.append({"day": day, "amount": round(s,2)})
        
    return jsonify({
        "total_earning": round(u['total_earning'],2), "upi_id": u['upi_id'],
        "email_status": u['email_status'], "email_last_check": u['email_last_check'],
        "total_api_keys": len(keys), "total_requests": sum(k['total_requests'] for k in keys),
        "total_qr_generated": sum(k['total_qr'] for k in keys),
        "total_received_via_api": round(sum(k['total_received'] for k in keys),2),
        "chart": chart
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

# ==========================================
# PUBLIC QR & VERIFICATION API
# ==========================================
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
    
    qr_image = qr_b64(upi)
    return render_template_string(QR_HTML_TEMPLATE, amount=amount, qr_b64=qr_image, order_id=order_id, upi_id=user['upi_id'], full_name=user['full_name'], minutes=minutes)

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

# ==========================================
# HTML TEMPLATES (Inside app.py)
# ==========================================

MAIN_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FamPay AutoQR — Neon</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<style>
:root{--bg:#05050a;--surface:#0f0f1e;--surface2:#15152a;--border:#1f1f3a;--text:#e8e8f5;--dim:#7a7a9a;
--ng:#00ffb3;--nc:#00d4ff;--np:#a855f7;--dang:#ff4757;--warn:#ffa502;}
*{margin:0;padding:0;box-sizing:border-box}body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:-1;background:radial-gradient(600px at 10% 5%,rgba(0,255,179,.08),transparent 60%),radial-gradient(600px at 90% 10%,rgba(168,85,247,.08),transparent 60%)}
button,input,select{font-family:inherit;outline:none}.hidden{display:none!important}
.auth-wrap{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.auth-card{width:100%;max-width:420px;background:var(--surface);border:1px solid var(--border);border-radius:22px;padding:34px 28px;box-shadow:0 0 40px rgba(0,255,179,.15)}
.logo{font-size:22px;font-weight:900;text-align:center;margin-bottom:4px;background:linear-gradient(90deg,var(--ng),var(--nc),var(--np));-webkit-background-clip:text;color:transparent}
.logo-sub{text-align:center;color:var(--dim);font-size:12px;margin-bottom:22px}
.field{margin-bottom:14px}.field label{display:block;font-size:11px;font-weight:700;color:var(--dim);margin-bottom:6px;text-transform:uppercase}
.field input{width:100%;padding:12px 14px;background:var(--surface2);border:1px solid var(--border);border-radius:10px;color:var(--text);font-size:14px;transition:.2s}
.field input:focus{border-color:var(--ng);box-shadow:0 0 0 3px rgba(0,255,179,.15)}
.btn{width:100%;padding:13px;border:none;border-radius:10px;font-weight:800;font-size:14px;background:linear-gradient(90deg,var(--ng),var(--nc));color:#05050a;transition:.2s;cursor:pointer}
.btn:hover{transform:translateY(-1px);box-shadow:0 0 30px rgba(0,255,179,.5)}
.btn.sec{background:var(--surface2);color:var(--text);border:1px solid var(--border)}
.btn.sec:hover{box-shadow:none;border-color:var(--ng)}
.switch{text-align:center;margin-top:14px;font-size:13px;color:var(--dim)}.switch a{color:var(--ng);font-weight:700;cursor:pointer}
.shell{display:flex;min-height:100vh}
.sidebar{width:260px;background:var(--surface);border-right:1px solid var(--border);padding:22px 14px;position:fixed;inset:0 auto 0 0;overflow-y:auto;z-index:50;transition:transform .3s}
.brand{font-size:17px;font-weight:900;padding:0 8px 20px;background:linear-gradient(90deg,var(--ng),var(--nc),var(--np));-webkit-background-clip:text;color:transparent}
.nav{display:flex;flex-direction:column;gap:3px}
.item{padding:11px 14px;border-radius:10px;color:var(--dim);font-size:13px;font-weight:600;display:flex;align-items:center;gap:10px;cursor:pointer;transition:.15s}
.item:hover{background:var(--surface2);color:var(--text)}.item.active{background:linear-gradient(90deg,rgba(0,255,179,.15),rgba(168,85,247,.08));color:var(--ng);box-shadow:inset 0 0 20px rgba(0,255,179,.1)}
.sec-lbl{font-size:10px;font-weight:800;color:#3a3a5a;text-transform:uppercase;letter-spacing:1.5px;padding:16px 14px 6px}
.main{flex:1;margin-left:260px;padding:24px;max-width:100%}
@media(max-width:860px){.sidebar{transform:translateX(-100%)}.sidebar.open{transform:none}.main{margin-left:0;padding:14px}}
.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:22px;padding-bottom:16px;border-bottom:1px solid var(--border);gap:12px}
.topbar h1{font-size:22px;font-weight:800}.chip{display:flex;align-items:center;gap:8px;padding:7px 14px;background:var(--surface);border:1px solid var(--border);border-radius:30px;font-size:12px;font-weight:600}
.hamburger{display:none;background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:8px 10px;border-radius:8px}
@media(max-width:860px){.hamburger{display:block}}
.dot{width:8px;height:8px;border-radius:50%;box-shadow:0 0 8px currentColor}.dot.on{background:var(--ng);color:var(--ng)}.dot.off{background:var(--dang);color:var(--dang)}.dot.unk{background:var(--dim);color:var(--dim)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:22px;margin-bottom:16px}
.card h2{font-size:15px;font-weight:700;margin-bottom:14px;display:flex;align-items:center;gap:8px}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px}.grid-3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.grid-4{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
@media(max-width:860px){.grid-2,.grid-3,.grid-4{grid-template-columns:1fr}}
.hero{background:linear-gradient(135deg,rgba(0,255,179,.12),rgba(168,85,247,.12),rgba(0,212,255,.1));border:1px solid rgba(0,255,179,.3);border-radius:20px;padding:26px;position:relative;overflow:hidden}
.hero-amt{font-size:48px;font-weight:900;margin:8px 0;background:linear-gradient(90deg,var(--ng),var(--nc),var(--np));-webkit-background-clip:text;color:transparent;filter:drop-shadow(0 0 12px rgba(0,255,179,.4))}
.hero-lbl{font-size:11px;font-weight:800;color:var(--dim);text-transform:uppercase;letter-spacing:2px}
.hero-sub{font-size:13px;color:var(--dim)}.hero-sub b{color:var(--text)}
.stat{background:var(--surface2);border:1px solid var(--border);border-radius:14px;padding:16px;position:relative;overflow:hidden}
.stat::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,var(--ng),var(--nc))}
.stat.alt::before{background:linear-gradient(90deg,var(--np),var(--nc))}.stat.alt2::before{background:linear-gradient(90deg,var(--warn),var(--ng))}
.stat-lbl{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:1px;font-weight:700}
.stat-val{font-size:24px;font-weight:800;margin-top:4px;color:var(--ng)}.stat.alt .stat-val{color:var(--np)}.stat.alt2 .stat-val{color:var(--warn)}
.chart{display:flex;align-items:flex-end;gap:8px;height:140px;padding-top:10px}
.bar-wrap{flex:1;display:flex;flex-direction:column;align-items:center;gap:6px}
.bar{width:100%;background:linear-gradient(180deg,var(--ng),rgba(0,255,179,.2));border-radius:6px 6px 0 0;min-height:4px;transition:.3s}
.bar-wrap:hover .bar{box-shadow:0 0 20px rgba(0,255,179,.6)}
.bar-label{font-size:10px;color:var(--dim)}.bar-val{font-size:10px;font-weight:700;color:var(--ng)}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
@media(max-width:600px){.form-grid{grid-template-columns:1fr}}
.fg{display:flex;flex-direction:column;gap:6px}.fg label{font-size:11px;font-weight:700;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
.fg input,.fg select{padding:11px 14px;background:var(--surface2);border:1px solid var(--border);border-radius:10px;color:var(--text);font-size:14px;transition:.2s}
.fg input:focus,.fg select:focus{border-color:var(--ng);box-shadow:0 0 0 3px rgba(0,255,179,.15)}
.api-out{background:#02020a;border:1px dashed rgba(0,255,179,.4);padding:12px;border-radius:10px;color:var(--ng);font-family:monospace;font-size:11px;word-break:break-all;margin-top:8px;display:flex;justify-content:space-between;align-items:center;gap:8px}
.copy{background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:5px 9px;border-radius:6px;font-size:10px;font-weight:700;flex-shrink:0;cursor:pointer}
.copy:hover{background:var(--ng);color:#05050a;border-color:var(--ng)}
.txn{display:flex;justify-content:space-between;align-items:center;padding:12px;background:var(--surface2);border-radius:10px;margin-bottom:6px;font-size:13px;border-left:3px solid var(--ng)}
.txn .amt{color:var(--ng);font-weight:800}.txn .meta{font-size:11px;color:var(--dim);margin-top:2px}
.flist{list-style:none}.flist li{padding:10px 0;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;font-size:13px}
.flist li:last-child{border:none}.flist li::before{content:'✦';color:var(--ng);font-size:14px}
#toast{position:fixed;bottom:20px;right:20px;padding:14px 20px;border-radius:12px;background:var(--surface2);color:var(--text);font-size:13px;font-weight:600;border-left:3px solid var(--ng);box-shadow:0 0 30px rgba(0,255,179,.3);transform:translateY(100px);opacity:0;transition:.3s;z-index:1000;max-width:340px}
#toast.show{transform:translateY(0);opacity:1}#toast.err{border-left-color:var(--dang);box-shadow:0 0 30px rgba(255,71,87,.3)}
.empty{text-align:center;padding:36px 20px;color:var(--dim);font-size:13px}.empty-ic{font-size:36px;opacity:.4;margin-bottom:8px}
.badge{display:inline-block;padding:3px 8px;border-radius:6px;font-size:10px;font-weight:800;text-transform:uppercase}
.badge.life{background:rgba(168,85,247,.15);color:var(--np)}
</style></head><body>

<div id="authScreen" class="auth-wrap">
  <div class="auth-card">
    <div class="logo">◆ FamPay AutoQR</div><div class="logo-sub">Neon Payment Verification</div>
    <div id="loginForm">
      <div class="field"><label>Username</label><input id="lgU" placeholder="admin"></div>
      <div class="field"><label>Password</label><input id="lgP" type="password" placeholder="••••••"></div>
      <button class="btn" onclick="doLogin()">Login →</button>
      <div class="switch">New? <a onclick="toggleAuth('reg')">Create account</a></div>
      <div style="font-size:11px;color:var(--dim);text-align:center;margin-top:8px">Demo: admin / admin123</div>
    </div>
    <div id="regForm" class="hidden">
      <div class="field"><label>Username</label><input id="rgU" placeholder="yourname"></div>
      <div class="field"><label>Password</label><input id="rgP" type="password" placeholder="min 4 chars"></div>
      <button class="btn" onclick="doReg()">Create Account →</button>
      <div class="switch">Have account? <a onclick="toggleAuth('login')">Login</a></div>
    </div>
  </div>
</div>

<div id="shell" class="shell hidden">
  <aside id="sidebar" class="sidebar">
    <div class="brand">◆ FamPay AutoQR</div>
    <nav class="nav">
      <div class="item active" data-p="dashboard" onclick="go('dashboard')">📊 Dashboard</div>
      <div class="item" data-p="account" onclick="go('account')">👤 Account</div>
      <div class="item" data-p="keys" onclick="go('keys')">🔑 API Keys</div>
      <div class="sec-lbl">Advance</div>
      <div class="item" data-p="history" onclick="go('history')">📜 Transactions</div>
      <div class="item" data-p="docs" onclick="go('docs')">📖 API Docs</div>
    </nav>
    <div style="margin-top:20px"><button class="btn sec" onclick="doLogout()">🚪 Logout</button></div>
  </aside>
  <main class="main">
    <div class="topbar">
      <div style="display:flex;align-items:center;gap:10px">
        <button class="hamburger" onclick="document.getElementById('sidebar').classList.toggle('open')">☰</button>
        <h1 id="title">Dashboard</h1>
      </div>
      <div class="chip"><span id="emailDot" class="dot unk"></span><span id="chipUser">-</span></div>
    </div>

    <section id="p-dashboard" class="page">
      <div class="hero">
        <div class="hero-lbl">💰 Total Earnings</div>
        <div class="hero-amt">₹ <span id="earn">0.00</span></div>
        <div class="hero-sub">UPI: <b id="upiTxt">-</b> · Email: <b id="emailTxt">-</b></div>
      </div>
      <div class="grid-3" style="margin-top:16px">
        <div class="stat"><div class="stat-lbl">API Keys</div><div class="stat-val" id="stKeys">0</div></div>
        <div class="stat alt"><div class="stat-lbl">Requests</div><div class="stat-val" id="stReq">0</div></div>
        <div class="stat alt2"><div class="stat-lbl">QRs</div><div class="stat-val" id="stQR">0</div></div>
      </div>
      <div class="card" style="margin-top:16px">
        <h2>📊 Last 7 Days</h2><div class="chart" id="chart"></div>
      </div>
    </section>

    <section id="p-account" class="page hidden">
      <div class="card">
        <h2>👤 Account Details</h2>
        <div class="form-grid">
          <div class="fg"><label>Full Name</label><input id="acName"></div>
          <div class="fg"><label>UPI ID</label><input id="acUpi"></div>
        </div>
        <div class="form-grid">
          <div class="fg"><label>Mobile</label><input id="acMob"></div>
          <div class="fg"><label>Gmail</label><input id="acEmail" type="email"></div>
        </div>
        <div class="form-grid">
          <div class="fg"><label>App Password</label><input id="acPass" type="password"></div>
          <div class="fg"><label>QR Minutes</label><input id="acMin" type="number" value="15"></div>
        </div>
        <div style="display:flex;gap:10px;margin-top:10px">
          <button class="btn" style="flex:1" onclick="saveAccount()">💾 Save</button>
          <button class="btn sec" style="flex:1" onclick="testEmail()">🔍 Test Email</button>
        </div>
        <div id="testRes" style="margin-top:12px"></div>
      </div>
    </section>

    <section id="p-keys" class="page hidden">
      <div class="card">
        <h2>🔑 Generate Key</h2>
        <div class="form-grid">
          <div class="fg"><label>Label</label><input id="keyLabel"></div>
          <div class="fg"><label>Expiry</label><select id="keyMode" onchange="toggleMode()"><option value="days">Days</option><option value="lifetime">Lifetime</option></select></div>
        </div>
        <div class="fg" id="daysWrap" style="margin-bottom:12px"><label>Days</label><input id="keyDays" type="number" value="30"></div>
        <button class="btn" onclick="genKey()">🚀 Generate</button>
        <div id="keyRes" style="margin-top:12px"></div>
      </div>
      <div class="card"><h2>📋 Your Keys</h2><div id="keysList"></div></div>
    </section>

    <section id="p-history" class="page hidden">
      <div class="card"><h2>📜 Transactions</h2><div id="txnList"></div></div>
    </section>

    <section id="p-docs" class="page hidden">
      <div class="card">
        <h2>📖 API Docs</h2>
        <div class="fg"><label>QR Endpoint</label><div class="api-out"><span>GET /v2/fam/API_KEY&qr=AMOUNT</span></div></div>
        <div class="fg" style="margin-top:12px"><label>Check Endpoint (JSON)</label><div class="api-out"><span>GET /api/v2/check/API_KEY</span></div></div>
      </div>
    </section>
  </main>
</div>
<div id="toast"></div>

<script>
function toast(m, err){const t=document.getElementById('toast');t.textContent=m;t.className='show'+(err?' err':'');clearTimeout(window.__t);window.__t=setTimeout(()=>t.className='',3000);}
function copyText(t){navigator.clipboard.writeText(t).then(()=>toast('✅ Copied'))}
async function api(p, o={}){const r=await fetch(p,{headers:{'Content-Type':'application/json'},credentials:'same-origin',...o});let d={};try{d=await r.json()}catch{}if(!r.ok)throw new Error(d.error||d.message||'Failed');return d;}

function toggleAuth(m){document.getElementById('loginForm').classList.toggle('hidden',m!=='login');document.getElementById('regForm').classList.toggle('hidden',m!=='reg');}
async function doLogin(){try{const u=document.getElementById('lgU').value.trim(),p=document.getElementById('lgP').value;if(!u||!p)return toast('Fill fields',true);await api('/api/login',{method:'POST',body:JSON.stringify({username:u,password:p})});toast('✅ Logged in');bootApp();}catch(e){toast('❌ '+e.message,true)}}
async function doReg(){try{const u=document.getElementById('rgU').value.trim(),p=document.getElementById('rgP').value;if(!u||!p)return toast('Fill fields',true);await api('/api/register',{method:'POST',body:JSON.stringify({username:u,password:p})});toast('✅ Created');toggleAuth('login');document.getElementById('lgU').value=u;}catch(e){toast('❌ '+e.message,true)}}
async function doLogout(){await api('/api/logout',{method:'POST'});location.reload();}

function go(p){
  document.querySelectorAll('.page').forEach(x=>x.classList.add('hidden'));
  document.getElementById('p-'+p).classList.remove('hidden');
  document.querySelectorAll('.item').forEach(i=>i.classList.toggle('active',i.dataset.p===p));
  const titles={dashboard:'Dashboard',account:'Account',keys:'API Keys',history:'Transactions',docs:'API Docs'};
  document.getElementById('title').textContent=titles[p]||p;
  document.getElementById('sidebar').classList.remove('open');
  if(p==='keys')loadKeys();if(p==='history')loadHistory();
}

async function bootApp(){
  try{
    const me=await api('/api/me');
    document.getElementById('authScreen').classList.add('hidden');
    document.getElementById('shell').classList.remove('hidden');
    document.getElementById('chipUser').textContent=me.username;
    const dot=document.getElementById('emailDot');
    dot.className='dot '+(me.email_status==='online'?'on':me.email_status==='offline'?'off':'unk');
    fillAccount(me);loadDashboard();
  }catch(e){location.reload();}
}

function fillAccount(me){
  document.getElementById('acName').value=me.full_name||'';document.getElementById('acUpi').value=me.upi_id||'';
  document.getElementById('acMob').value=me.mobile||'';document.getElementById('acEmail').value=me.email||'';
  document.getElementById('acMin').value=me.default_qr_minutes||15;
  document.getElementById('emailTxt').textContent=me.email||'-';document.getElementById('upiTxt').textContent=me.upi_id||'-';
}

async function loadDashboard(){try{
  const d=await api('/api/dashboard');
  document.getElementById('earn').textContent=d.total_earning.toFixed(2);
  document.getElementById('stKeys').textContent=d.total_api_keys;document.getElementById('stReq').textContent=d.total_requests;document.getElementById('stQR').textContent=d.total_qr_generated;
  const ch=document.getElementById('chart'),cd=d.chart||[],max=Math.max(...cd.map(x=>x.amount),1);
  ch.innerHTML=cd.map(x=>{const h=Math.max(4,(x.amount/max)*100),day=new Date(x.day).toLocaleDateString('en',{weekday:'short'});return `<div class="bar-wrap"><div class="bar-val">₹${x.amount.toFixed(0)}</div><div class="bar" style="height:${h}%"></div><div class="bar-label">${day}</div></div>`;}).join('');
}catch(e){toast('❌ '+e.message,true)}}

async function saveAccount(){try{
  const payload={full_name:document.getElementById('acName').value.trim(),upi_id:document.getElementById('acUpi').value.trim(),mobile:document.getElementById('acMob').value.trim(),email:document.getElementById('acEmail').value.trim(),app_password:document.getElementById('acPass').value.trim(),default_qr_minutes:parseInt(document.getElementById('acMin').value)||15};
  if(!payload.upi_id)return toast('UPI required',true);
  await api('/api/setup-account',{method:'POST',body:JSON.stringify(payload)});toast('✅ Saved');document.getElementById('acPass').value='';bootApp();
}catch(e){toast('❌ '+e.message,true)}}

async function testEmail(){try{
  document.getElementById('testRes').innerHTML='<div style="color:var(--warn)">⏳ Testing…</div>';
  const d=await api('/api/test-email',{method:'POST',body:JSON.stringify({email:document.getElementById('acEmail').value,app_password:document.getElementById('acPass').value})});
  document.getElementById('testRes').innerHTML=`<div style="background:rgba(0,255,179,.1);border:1px solid rgba(0,255,179,.4);padding:12px;border-radius:10px;color:var(--ng);font-size:13px">${d.message}<br><b>Total:</b> ${d.total_emails||0} | <b>Unseen:</b> ${d.unseen_emails||0}</div>`;
  document.getElementById('emailDot').className='dot on';
}catch(e){document.getElementById('testRes').innerHTML=`<div style="background:rgba(255,71,87,.1);border:1px solid rgba(255,71,87,.4);padding:12px;border-radius:10px;color:var(--dang);font-size:13px">${e.message}</div>`;document.getElementById('emailDot').className='dot off';}}

function toggleMode(){document.getElementById('daysWrap').style.display=document.getElementById('keyMode').value==='lifetime'?'none':'flex';}
async function genKey(){try{
  const mode=document.getElementById('keyMode').value,days=parseInt(document.getElementById('keyDays').value)||30;
  const d=await api('/api/generate-key',{method:'POST',body:JSON.stringify({mode,days,label:document.getElementById('keyLabel').value})});
  document.getElementById('keyRes').innerHTML=`<div style="background:rgba(0,255,179,.08);border:1px solid rgba(0,255,179,.4);padding:14px;border-radius:10px"><div style="color:var(--ng);font-weight:700;margin-bottom:6px">✅ Key Generated</div><div class="api-out"><span>${d.example}</span><button class="copy" onclick="copyText('${d.example}')">Copy</button></div></div>`;
  loadKeys();loadDashboard();
}catch(e){toast('❌ '+e.message,true)}}

async function loadKeys(){try{
  const list=await api('/api/list-keys'),el=document.getElementById('keysList');
  if(!list.length){el.innerHTML='<div class="empty"><div class="empty-ic">🔑</div>No keys</div>';return}
  el.innerHTML=list.map(k=>`<div class="txn" style="flex-direction:column;align-items:stretch;gap:8px;padding:14px"><div style="display:flex;justify-content:space-between"><div><b>${k.api_key}</b> ${k.expiry==='LIFETIME'?'<span class="badge life">Life</span>':''}</div><button class="copy" style="background:rgba(255,71,87,.15);color:var(--dang)" onclick="delKey('${k.api_key}')">Del</button></div><div class="meta">Req:${k.total_requests} QR:${k.total_qr} ₹${k.total_received}</div><div class="api-out"><span>${k.url}</span><button class="copy" onclick="copyText('${k.url}')">Copy</button></div></div>`).join('');
}catch(e){toast('❌ '+e.message,true)}}

async function delKey(k){if(!confirm('Delete?'))return;try{await api('/api/delete-key/'+k,{method:'DELETE'});toast('Deleted');loadKeys()}catch(e){toast('❌ '+e.message,true)}}

async function loadHistory(){try{
  const d=await api('/api/dashboard'),el=document.getElementById('txnList'),txns=d.transactions||[];
  if(!txns.length){el.innerHTML='<div class="empty"><div class="empty-ic">📜</div>No transactions</div>';return}
  el.innerHTML=txns.map(t=>`<div class="txn"><div><div class="amt">+ ₹${t.amount.toFixed(2)}</div><div class="meta">${t.txn_id||'-'}</div></div><div style="font-size:11px;color:var(--dim)">${t.order_id||''}</div></div>`).join('');
}catch(e){toast('❌ '+e.message,true)}}

(async()=>{try{await api('/api/me');bootApp()}catch{}})();
</script></body></html>
"""

QR_HTML_TEMPLATE = """
<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pay ₹{{ amount }}</title><link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:'Inter',sans-serif}
body{background:#05050a;color:#e8e8f5;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;background:radial-gradient(circle at 30% 20%,rgba(0,224,164,.15),transparent 45%),radial-gradient(circle at 70% 80%,rgba(0,148,255,.15),transparent 45%),#05050a}
.card{width:100%;max-width:430px;background:#0f0f1e;border:1px solid #1f1f3a;border-radius:24px;padding:30px 24px;box-shadow:0 0 60px rgba(0,224,164,.12);text-align:center}
.tag{font-size:12px;color:#7a7a9a;text-transform:uppercase;letter-spacing:2px;margin-bottom:6px}
h1{font-size:44px;font-weight:800;margin:6px 0;background:linear-gradient(90deg,#00e0a4,#0094ff,#a855f7);-webkit-background-clip:text;color:transparent}
.qr-wrap{background:#fff;border-radius:18px;padding:12px;display:inline-block;margin:16px 0;box-shadow:0 0 50px rgba(0,224,164,.3);position:relative}
.qr-wrap img{display:block;width:240px;height:240px}
.qr-wrap.verified::after{content:'✅';position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:130px;background:rgba(0,224,164,.9);border-radius:18px}
.logos{display:flex;justify-content:center;gap:8px;margin:14px 0;flex-wrap:wrap}
.logos span{background:#fff;color:#000;padding:6px 11px;border-radius:8px;font-weight:700;font-size:11px}
.timer{font-size:30px;font-weight:800;margin:10px 0;color:#00e0a4;letter-spacing:1px;font-variant-numeric:tabular-nums}
.status{padding:14px;border-radius:12px;font-weight:600;font-size:14px;margin-top:8px}
.pending{background:rgba(255,165,2,.12);color:#ffa502}
.paid{background:rgba(0,224,164,.15);color:#00e0a4}
.expired{background:rgba(255,71,87,.15);color:#ff4757}
.upi{font-size:12px;color:#7a7a9a;margin-top:8px}.upi b{color:#e8e8f5}
</style></head><body>
<div class="card"><div class="tag">Pay via UPI</div><h1>₹{{ "%.2f"|format(amount) }}</h1>
<div class="upi">to <b>{{ full_name }}</b> · {{ upi_id }}</div>
<div class="qr-wrap" id="qrWrap"><img src="{{ qr_b64 }}"></div>
<div class="logos"><span>GPay</span><span>PhonePe</span><span>Paytm</span><span>UPI</span></div>
<div class="timer" id="timer">{{ '%02d:00'|format(minutes) }}</div>
<div class="status pending" id="status">⏳ Waiting for payment...</div></div>
<script>
const orderId="{{ order_id }}",totalSecs={{ minutes*60 }};let secs=totalSecs,tInt,pInt;
const T=document.getElementById('timer'),S=document.getElementById('status');
function tick(){secs--;if(secs<=0){T.textContent='00:00';T.style.color='#ff4757';S.textContent='❌ QR Expired';S.className='status expired';clearInterval(tInt);clearInterval(pInt);return}
const m=String(Math.floor(secs/60)).padStart(2,'0'),s=String(secs%60).padStart(2,'0');T.textContent=m+':'+s;if(secs<=60)T.style.color='#ff4757';}
async function poll(){try{const r=await fetch('/api/check-payment/'+orderId),d=await r.json();
if(d.status==='PAID'){S.textContent='✅ Verified! Txn: '+(d.txn?.txn_id||'');S.className='status paid';document.getElementById('qrWrap').classList.add('verified');T.textContent='PAID';T.style.color='#00e0a4';clearInterval(tInt);clearInterval(pInt);}
else if(d.status==='EXPIRED'){S.textContent='❌ QR Expired';S.className='status expired';clearInterval(tInt);clearInterval(pInt);}}catch(e){}}
tInt=setInterval(tick,1000);pInt=setInterval(poll,5000);poll();
</script></body></html>
"""

@app.route('/')
def home(): return render_template_string(MAIN_HTML)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
