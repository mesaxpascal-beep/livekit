"""AfroVibe - réseau social vidéo rémunéré (prototype).

Lancer :   pip install flask
           python app.py
Ouvrir :   http://localhost:5000   (ou http://IP-DE-TON-PC:5000 depuis ton téléphone, même Wi-Fi)
"""
import os, sqlite3, time
from datetime import date, timedelta
from functools import wraps
from flask import Flask, g, request, session, jsonify, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from livekit import api as lk_api   # pip install livekit-api

# ---------- Réglages économiques (à ajuster) ----------
APP_NAME = "AfroVibe"
DAILY_GOAL = 50        # likes par jour pour toucher le bonus
POINTS_PER_LIKE = 1    # points gagnés par like (jusqu'à l'objectif)
GOAL_BONUS = 50        # bonus de points quand l'objectif du jour est atteint
POINTS_PER_FCFA = 5    # 5 points = 1 FCFA
COIN_FCFA = 5          # 1 pièce achetée = 5 FCFA
DIAMOND_FCFA = 5       # 1 diamant reçu = 5 FCFA (le créateur touche 50 %, la plateforme garde 50 %)
MIN_WITHDRAW = 500     # retrait minimum en FCFA
DEMO_MODE = True       # True : la recharge est gratuite. Mettre False une fois Mobile Money branché.
GIFTS = {"rose": ("🌹", 5), "coeur": ("💖", 20), "couronne": ("👑", 100), "fusee": ("🚀", 500)}

# ---------- LiveKit (vrai live vidéo) ----------
# Ne mets JAMAIS le secret en clair ici. Lance plutôt :
#   export LIVEKIT_URL="wss://live-107jmi93.livekit.cloud"
#   export LIVEKIT_API_KEY="APIajLbvfCf4EPW"
#   export LIVEKIT_API_SECRET="colle-ton-secret-ici"
# avant "python app.py" (ou mets-les dans un fichier .env chargé par ton hébergeur).
LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "wss://live-107jmi93.livekit.cloud")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "APIajLbvfCf4EPW")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET")  # obligatoire, pas de valeur par défaut

# Sur l'hébergeur, mets DATA_DIR sur le disque persistant (ex. /data), sinon les données sont perdues à chaque redémarrage.
BASE = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
DB, UP = os.path.join(BASE, "afrovibe.db"), os.path.join(BASE, "uploads")
os.makedirs(UP, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-moi-en-production")
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT UNIQUE, pw TEXT,
  points INT DEFAULT 0, coins INT DEFAULT 0, diamonds INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS videos(id INTEGER PRIMARY KEY, uid INT, title TEXT, file TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS likes(uid INT, vid INT, day TEXT, PRIMARY KEY(uid, vid));
CREATE TABLE IF NOT EXISTS lives(id INTEGER PRIMARY KEY, uid INT, title TEXT, room TEXT, active INT DEFAULT 1, ts REAL);
"""


def lk_token(room, identity, name, publish):
    """Génère un jeton LiveKit pour rejoindre `room` (hôte = publish True, spectateur = False)."""
    if not LIVEKIT_API_SECRET:
        raise RuntimeError("LIVEKIT_API_SECRET manquant : exporte-le avant de lancer l'app.")
    grants = lk_api.VideoGrants(room_join=True, room=room, can_publish=publish,
                                 can_subscribe=True, can_publish_data=True)
    t = (lk_api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
         .with_identity(identity).with_name(name).with_ttl(timedelta(hours=4))
         .with_grants(grants))
    return t.to_jwt()


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()


def me():
    uid = session.get("uid")
    return db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() if uid else None


def need(f):
    @wraps(f)
    def w(*a, **k):
        if not me():
            return jsonify(error="Connecte-toi d'abord"), 401
        return f(*a, **k)
    return w


def stats(u):
    n = db().execute("SELECT COUNT(*) FROM likes WHERE uid=? AND day=?",
                     (u["id"], date.today().isoformat())).fetchone()[0]
    return dict(name=u["name"], points=u["points"], coins=u["coins"], diamonds=u["diamonds"],
                likes_today=n, goal=DAILY_GOAL,
                fcfa=u["points"] // POINTS_PER_FCFA + u["diamonds"] * DIAMOND_FCFA)


# ---------- API ----------
@app.post("/api/auth")
def auth():
    d = request.get_json(silent=True) or {}
    name, pw = (d.get("name") or "").strip()[:20], d.get("pw") or ""
    if len(name) < 3 or len(pw) < 4:
        return jsonify(error="Pseudo (3 caractères min) et mot de passe (4 min) requis"), 400
    u = db().execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
    if u and not check_password_hash(u["pw"], pw):
        return jsonify(error="Mot de passe incorrect"), 400
    if not u:  # Inscription automatique. En production : vérifier un numéro de téléphone (anti-fraude).
        cur = db().execute("INSERT INTO users(name,pw) VALUES(?,?)", (name, generate_password_hash(pw)))
        db().commit()
        session["uid"] = cur.lastrowid
    else:
        session["uid"] = u["id"]
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def api_me():
    u = me()
    return jsonify(stats(u) if u else None)


@app.get("/api/feed")
def feed():
    u = me()
    rows = db().execute("""SELECT v.id, v.title, v.file, us.name author,
        (SELECT COUNT(*) FROM likes WHERE vid=v.id) likes,
        EXISTS(SELECT 1 FROM likes WHERE vid=v.id AND uid=?) liked
        FROM videos v JOIN users us ON us.id=v.uid ORDER BY v.ts DESC LIMIT 50""",
                       (u["id"] if u else 0,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/like/<int:vid>")
@need
def like(vid):
    u, c = me(), db()
    if c.execute("SELECT 1 FROM likes WHERE uid=? AND vid=?", (u["id"], vid)).fetchone():
        return jsonify(error="Tu as déjà liké cette vidéo"), 400
    c.execute("INSERT INTO likes VALUES(?,?,?)", (u["id"], vid, date.today().isoformat()))
    n = c.execute("SELECT COUNT(*) FROM likes WHERE uid=? AND day=?",
                  (u["id"], date.today().isoformat())).fetchone()[0]
    gain = POINTS_PER_LIKE if n <= DAILY_GOAL else 0   # plafond quotidien
    if n == DAILY_GOAL:
        gain += GOAL_BONUS
    c.execute("UPDATE users SET points=points+? WHERE id=?", (gain, u["id"]))
    c.commit()
    return jsonify(gain=gain, me=stats(me()))


@app.post("/api/upload")
@need
def upload():
    f = request.files.get("video")
    if not f or not f.filename.lower().endswith((".mp4", ".webm", ".mov")):
        return jsonify(error="Choisis une vidéo .mp4, .webm ou .mov"), 400
    name = f"{int(time.time() * 1000)}_{secure_filename(f.filename)}"
    f.save(os.path.join(UP, name))
    title = (request.form.get("title") or "Sans titre")[:80]
    db().execute("INSERT INTO videos(uid,title,file,ts) VALUES(?,?,?,?)", (me()["id"], title, name, time.time()))
    db().commit()
    return jsonify(ok=True)


@app.get("/media/<path:n>")
def media(n):
    return send_from_directory(UP, n)


@app.get("/api/lives")
def lives():
    rows = db().execute("""SELECT l.id, l.title, l.room, u.name host FROM lives l
        JOIN users u ON u.id=l.uid WHERE l.active=1 ORDER BY l.ts DESC""").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/live")
@need
def live_start():
    t = ((request.get_json(silent=True) or {}).get("title") or "Live de " + me()["name"])[:60]
    room = f"live-{me()['id']}-{int(time.time())}"
    cur = db().execute("INSERT INTO lives(uid,title,room,ts) VALUES(?,?,?,?)", (me()["id"], t, room, time.time()))
    db().commit()
    return jsonify(ok=True, lid=cur.lastrowid)


@app.post("/api/live/<int:lid>/end")
@need
def live_end(lid):
    db().execute("UPDATE lives SET active=0 WHERE id=? AND uid=?", (lid, me()["id"]))
    db().commit()
    return jsonify(ok=True)


@app.get("/api/live/<int:lid>/token")
@need
def live_token(lid):
    u = me()
    l = db().execute("SELECT * FROM lives WHERE id=? AND active=1", (lid,)).fetchone()
    if not l:
        return jsonify(error="Ce live est terminé"), 404
    try:
        tok = lk_token(l["room"], f"u{u['id']}", u["name"], publish=(l["uid"] == u["id"]))
    except RuntimeError as e:
        return jsonify(error=str(e)), 500
    return jsonify(token=tok, url=LIVEKIT_URL, host=(l["uid"] == u["id"]))


@app.post("/api/live/<int:lid>/gift/<k>")
@need
def gift(lid, k):
    if k not in GIFTS:
        return jsonify(error="Cadeau inconnu"), 400
    u, (emoji, price) = me(), GIFTS[k]
    l = db().execute("SELECT * FROM lives WHERE id=? AND active=1", (lid,)).fetchone()
    if not l:
        return jsonify(error="Ce live est terminé"), 404
    if l["uid"] == u["id"]:
        return jsonify(error="Tu ne peux pas t'envoyer un cadeau"), 400
    if u["coins"] < price:
        return jsonify(error="Pas assez de pièces : recharge dans l'onglet Gains"), 400
    db().execute("UPDATE users SET coins=coins-? WHERE id=?", (price, u["id"]))
    db().execute("UPDATE users SET diamonds=diamonds+? WHERE id=?", (price // 2, l["uid"]))  # commission 50 %
    db().commit()
    return jsonify(emoji=emoji, me=stats(me()))


@app.post("/api/recharge")
@need
def recharge():
    n = int((request.get_json(silent=True) or {}).get("coins", 0))
    if n not in (50, 100, 500):
        return jsonify(error="Pack invalide"), 400
    if not DEMO_MODE:
        return jsonify(error="Paiement Mobile Money à brancher"), 501
    # TODO : débiter n * COIN_FCFA via T-Money / Flooz avant de créditer.
    db().execute("UPDATE users SET coins=coins+? WHERE id=?", (n, me()["id"]))
    db().commit()
    return jsonify(me=stats(me()))


@app.post("/api/withdraw")
@need
def withdraw():
    if DEMO_MODE:
        return jsonify(error="Retrait désactivé en mode démo"), 403
    u = me()
    fcfa = u["points"] // POINTS_PER_FCFA + u["diamonds"] * DIAMOND_FCFA
    if fcfa < MIN_WITHDRAW:
        return jsonify(error=f"Retrait minimum : {MIN_WITHDRAW} FCFA (tu as {fcfa} FCFA)"), 400
    db().execute("UPDATE users SET points=points%?, diamonds=0 WHERE id=?", (POINTS_PER_FCFA, u["id"]))
    db().commit()
    # TODO : envoyer `fcfa` via l'API Mobile Money après vérification d'identité.
    return jsonify(msg=f"Retrait de {fcfa} FCFA demandé", me=stats(me()))


# ---------- Interface ----------
INDEX = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>__APP__</title>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@400;600;800&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/livekit-client@2.18.3/dist/livekit-client.umd.min.js"></script>
<style>
:root{--bg:#0e0b2e;--sf:#1a1550;--ln:#2e2872;--tx:#f3f0ff;--mu:#9b95d0;--sun:#ffb627;--hib:#ff3d7f;--lag:#2ee6c5}
*{box-sizing:border-box;margin:0;-webkit-tap-highlight-color:transparent}
button,input{font:inherit}
html,body{height:100%;background:var(--bg)}
body{color:var(--tx);font:15px/1.4 'Bricolage Grotesque',system-ui,sans-serif;display:flex;justify-content:center;
background-image:radial-gradient(#ffffff0d 1.2px,transparent 1.4px);background-size:22px 22px}
#app{width:100%;max-width:460px;height:100dvh;display:flex;flex-direction:column;background:var(--bg);border-inline:1px solid var(--ln);position:relative;overflow:hidden}
header{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;padding-top:max(12px,env(safe-area-inset-top))}
.logo{font-weight:800;font-size:24px;letter-spacing:-.8px}
.goal{display:flex;align-items:center;gap:8px;background:var(--sf);border:1px solid var(--ln);border-radius:99px;padding:4px 14px 4px 4px;font-weight:600;cursor:pointer}
.goal svg{width:34px;height:34px;transform:rotate(-90deg)}
.goal circle{fill:none;stroke-width:4}
.goal .bg{stroke:var(--ln)}
.goal .fg{stroke:var(--lag);stroke-linecap:round;stroke-dasharray:94.2;stroke-dashoffset:94.2;transition:stroke-dashoffset .5s}
.v{display:none;flex:1;min-height:0;overflow-y:auto;padding:0 16px 16px}
.v.on{display:block}
#feed{padding:0;scroll-snap-type:y mandatory}
.card{height:100%;scroll-snap-align:start;position:relative;background:#000}
.card video{width:100%;height:100%;object-fit:cover}
.meta{position:absolute;left:0;right:70px;bottom:0;padding:56px 16px 20px;background:linear-gradient(transparent,#0e0b2eee)}
.meta h3{font-size:18px}.meta span{color:var(--mu)}
.like{position:absolute;right:12px;bottom:28px;width:52px;text-align:center;font-weight:600;background:none;border:0;color:var(--tx);font-size:13px;cursor:pointer}
.like i{display:grid;place-items:center;width:52px;height:52px;border-radius:50%;background:#1a1550cc;border:1px solid var(--ln);font-style:normal;font-size:24px;margin-bottom:4px;transition:transform .2s}
.like.on i{background:var(--hib);border-color:var(--hib);transform:scale(1.12)}
nav{display:flex;justify-content:space-around;align-items:center;padding:8px 8px max(8px,env(safe-area-inset-bottom));border-top:1px solid var(--ln)}
nav button{background:none;border:0;color:var(--mu);font-size:12px;font-weight:600;display:grid;gap:2px;justify-items:center;padding:6px 14px;cursor:pointer}
nav button span{font-size:22px}
nav button.on{color:var(--tx)}
nav .add span{background:var(--sun);color:var(--bg);border-radius:14px;width:46px;height:34px;display:grid;place-items:center;font-weight:800;font-size:24px}
.box{background:var(--sf);border:1px solid var(--ln);border-radius:18px;padding:16px;margin-top:12px}
.btn{width:100%;border:0;border-radius:14px;padding:13px;font-weight:700;font-size:15px;background:var(--sun);color:var(--bg);cursor:pointer}
.btn.alt{background:transparent;border:1px solid var(--ln);color:var(--tx)}
input{width:100%;background:var(--bg);border:1px solid var(--ln);border-radius:12px;padding:12px;color:var(--tx);margin-bottom:10px}
:focus-visible{outline:2px solid var(--lag);outline-offset:2px}
h2{font-size:28px;letter-spacing:-.8px;margin:10px 0 4px}
.mu{color:var(--mu)}
.big{font-size:38px;font-weight:800;color:var(--sun);letter-spacing:-1px}
.row{display:flex;gap:10px;margin-top:10px}.row>*{flex:1}
.live{cursor:pointer}
.tag{background:var(--hib);color:#fff;border-radius:99px;padding:2px 10px;font-size:12px;font-weight:700;margin-right:8px}
#room{position:absolute;inset:0;background:var(--bg);z-index:6;display:none;flex-direction:column;padding:16px;gap:12px}
#room.on{display:flex}
.stage{flex:1;border-radius:22px;border:1px solid var(--ln);display:grid;place-content:center;text-align:center;overflow:hidden;position:relative;
background:repeating-radial-gradient(circle at 50% 45%,var(--sf) 0 14px,var(--bg) 14px 28px)}
.stage video{width:100%;height:100%;object-fit:cover;position:absolute;inset:0}
.stage p{background:#0e0b2ee8;padding:14px 22px;border-radius:14px;font-size:17px;position:relative;z-index:1}
.gifts{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.gifts button{background:var(--sf);border:1px solid var(--ln);border-radius:14px;padding:10px 0;color:var(--tx);font-size:28px;cursor:pointer}
.gifts small{display:block;font-size:12px;color:var(--sun);font-weight:600}
.fly{position:absolute;bottom:150px;left:calc(50% - 30px);font-size:60px;animation:fly 1.6s ease-out forwards;pointer-events:none}
@keyframes fly{to{transform:translateY(-340px) scale(1.5);opacity:0}}
@media(prefers-reduced-motion:reduce){.fly{animation-duration:.01s}.goal .fg{transition:none}}
#auth{position:absolute;inset:0;background:#0e0b2ef5;z-index:8;display:none;place-items:center}
#auth.on{display:grid}
#toast{position:absolute;top:70px;left:50%;transform:translateX(-50%);background:var(--sf);border:1px solid var(--sun);padding:10px 18px;border-radius:99px;opacity:0;transition:opacity .3s;pointer-events:none;z-index:9;font-weight:600;white-space:nowrap}
#toast.on{opacity:1}
</style></head><body><div id="app">
<header><div class="logo">__APP__</div>
<div class="goal" onclick="tab('wallet')" title="Objectif du jour"><svg viewBox="0 0 36 36"><circle class="bg" cx="18" cy="18" r="15"/><circle class="fg" id="ring" cx="18" cy="18" r="15"/></svg><span id="gtxt">0/50</span></div></header>

<section id="feed" class="v on"></section>
<section id="live" class="v"><h2>En direct</h2><p class="mu">Envoie des cadeaux à tes créateurs préférés.</p><div id="lives"></div>
<button class="btn alt" style="margin-top:14px" onclick="startLive()">Lancer mon live</button></section>
<section id="add" class="v"><h2>Publier une vidéo</h2><div class="box">
<input id="ttl" placeholder="Titre de la vidéo" maxlength="80"><input id="file" type="file" accept="video/*"><button class="btn" onclick="upload()">Publier</button></div></section>
<section id="wallet" class="v"><h2 id="who"></h2>
<div class="box"><div class="mu">Solde retirable</div><div class="big" id="fc">0 FCFA</div><div class="mu" id="det"></div>
<button class="btn" style="margin-top:14px" onclick="withdraw()">Retirer mes gains</button></div>
<div class="box"><b>Recharger des pièces</b><div class="mu" id="coins"></div>
<div class="row"><button class="btn alt" onclick="recharge(50)">50 pièces<br>250 FCFA</button><button class="btn alt" onclick="recharge(100)">100 pièces<br>500 FCFA</button><button class="btn alt" onclick="recharge(500)">500 pièces<br>2 500 FCFA</button></div></div>
<button class="btn alt" style="margin-top:14px" onclick="logout()">Se déconnecter</button></section>

<nav><button data-t="feed" class="on" onclick="tab('feed')"><span>🏠</span>Fil</button>
<button data-t="live" onclick="tab('live')"><span>📡</span>Lives</button>
<button class="add" data-t="add" onclick="tab('add')"><span>+</span>Publier</button>
<button data-t="wallet" onclick="tab('wallet')"><span>💰</span>Gains</button></nav>

<div id="room"><div class="stage"><p id="rt"></p></div><div class="gifts" id="gifts"></div>
<button class="btn alt" onclick="closeRoom()">Quitter le live</button></div>
<div id="auth"><div class="box" style="width:88%"><h2>Rejoins __APP__</h2>
<p class="mu" style="margin-bottom:14px">Like des vidéos, suis des lives et gagne des points chaque jour.</p>
<input id="nm" placeholder="Pseudo" autocomplete="username"><input id="pw" type="password" placeholder="Mot de passe" autocomplete="current-password">
<button class="btn" onclick="login()">Continuer</button></div></div>
<div id="toast"></div></div>
<script>
const $=s=>document.querySelector(s),esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const GIFTS={rose:['🌹',5],coeur:['💖',20],couronne:['👑',100],fusee:['🚀',500]};
let LV=[],room=0,lkRoom=null;
async function api(u,b){const o={};if(b){o.method='POST';if(b instanceof FormData)o.body=b;else{o.headers={'Content-Type':'application/json'};o.body=JSON.stringify(b)}}
const r=await fetch(u,o),j=await r.json().catch(()=>({}));if(r.status==401)$('#auth').classList.add('on');if(!r.ok)throw new Error(j.error||'Erreur');return j}
function toast(t){const e=$('#toast');e.textContent=t;e.classList.add('on');clearTimeout(e.t);e.t=setTimeout(()=>e.classList.remove('on'),2400)}
function setMe(m){if(!m)return;$('#ring').style.strokeDashoffset=94.2*(1-Math.min(m.likes_today/m.goal,1));$('#gtxt').textContent=m.likes_today+'/'+m.goal;
$('#who').textContent='Salut '+m.name;$('#fc').textContent=m.fcfa+' FCFA';$('#det').textContent=m.points+' points et '+m.diamonds+' diamants';$('#coins').textContent='Tu as '+m.coins+' pièces'}
function tab(n){document.querySelectorAll('.v').forEach(e=>e.classList.toggle('on',e.id==n));document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.t==n));
if(n=='live')loadLives();if(n=='wallet')refresh()}
async function refresh(){const m=await fetch('/api/me').then(r=>r.json());if(m){setMe(m);$('#auth').classList.remove('on')}else $('#auth').classList.add('on')}
async function loadFeed(){const v=await api('/api/feed');$('#feed').innerHTML=v.length?v.map(x=>`<div class="card"><video src="/media/${encodeURI(x.file)}" loop muted playsinline preload="metadata"></video>
<div class="meta"><h3>${esc(x.title)}</h3><span>@${esc(x.author)}</span></div><button class="like ${x.liked?'on':''}" onclick="like(${x.id},this)" aria-label="Liker"><i>♥</i><b>${x.likes}</b></button></div>`).join('')
:'<div class="box" style="margin:16px"><h2>Le fil est vide</h2><p class="mu">Publie la première vidéo avec le bouton +.</p></div>';watch()}
function watch(){const o=new IntersectionObserver(es=>es.forEach(e=>e.isIntersecting?e.target.play().catch(()=>{}):e.target.pause()),{threshold:.7});
document.querySelectorAll('video').forEach(v=>{o.observe(v);v.onclick=()=>v.muted=!v.muted})}
async function like(id,b){try{const r=await api('/api/like/'+id,{});b.classList.add('on');const n=b.querySelector('b');n.textContent=+n.textContent+1;setMe(r.me);if(r.gain)toast('+'+r.gain+' points')}catch(e){toast(e.message)}}
async function loadLives(){LV=await api('/api/lives');$('#lives').innerHTML=LV.length?LV.map((x,i)=>`<div class="box live" onclick="openRoom(${i})"><span class="tag">En direct</span><b>${esc(x.title)}</b><div class="mu">@${esc(x.host)}</div></div>`).join(''):'<div class="box mu">Aucun live pour le moment. Lance le premier.</div>'}
async function openRoom(i){room=LV[i].id;$('#rt').innerHTML='<b>'+esc(LV[i].title)+'</b><br><span class="mu">@'+esc(LV[i].host)+'</span>';
$('#gifts').innerHTML=Object.entries(GIFTS).map(([k,[e,p]])=>`<button onclick="gift('${k}')" aria-label="Envoyer ${k}">${e}<small>${p} pièces</small></button>`).join('');
$('#room').classList.add('on');await joinLive(room)}
async function joinLive(lid){
  try{
    const j=await api('/api/live/'+lid+'/token');
    lkRoom=new LivekitClient.Room();
    lkRoom.on(LivekitClient.RoomEvent.TrackSubscribed,(track)=>{const el=track.attach();el.autoplay=true;el.playsInline=true;$('.stage').appendChild(el)});
    lkRoom.on(LivekitClient.RoomEvent.TrackUnsubscribed,(track)=>track.detach().forEach(e=>e.remove()));
    await lkRoom.connect(j.url,j.token);
    if(j.host){const cam=await LivekitClient.createLocalVideoTrack();const el=cam.attach();el.autoplay=true;el.muted=true;el.playsInline=true;$('.stage').appendChild(el);await lkRoom.localParticipant.publishTrack(cam);
      const mic=await LivekitClient.createLocalAudioTrack();await lkRoom.localParticipant.publishTrack(mic)}
  }catch(e){toast('Vidéo indisponible : '+e.message)}
}
async function closeRoom(){if(lkRoom){lkRoom.disconnect();lkRoom=null}document.querySelectorAll('.stage video,.stage audio').forEach(v=>v.remove());
if(room)try{await api('/api/live/'+room+'/end',{})}catch(e){}$('#room').classList.remove('on')}
async function gift(k){try{const r=await api(`/api/live/${room}/gift/${k}`,{});setMe(r.me);const f=document.createElement('div');f.className='fly';f.textContent=r.emoji;$('#room').append(f);setTimeout(()=>f.remove(),1700)}catch(e){toast(e.message)}}
async function startLive(){try{const t=prompt('Titre du live ?')||'';const r=await api('/api/live',{title:t});toast('Ton live est lancé');
await loadLives();const idx=LV.findIndex(x=>x.id===r.lid);if(idx>-1)openRoom(idx)}catch(e){toast(e.message)}}
async function upload(){const f=$('#file').files[0];if(!f)return toast('Choisis une vidéo');const d=new FormData();d.append('video',f);d.append('title',$('#ttl').value);
try{await api('/api/upload',d);toast('Vidéo publiée');$('#file').value=$('#ttl').value='';await loadFeed();tab('feed')}catch(e){toast(e.message)}}
async function recharge(n){try{setMe((await api('/api/recharge',{coins:n})).me);toast(n+' pièces ajoutées')}catch(e){toast(e.message)}}
async function withdraw(){try{const r=await api('/api/withdraw',{});setMe(r.me);toast(r.msg)}catch(e){toast(e.message)}}
async function logout(){await api('/api/logout',{});$('#auth').classList.add('on')}
async function login(){try{await api('/api/auth',{name:$('#nm').value,pw:$('#pw').value});$('#pw').value='';await refresh();loadFeed()}catch(e){toast(e.message)}}
refresh();loadFeed();
</script></body></html>""".replace("__APP__", APP_NAME)


@app.get("/")
def index():
    return INDEX


with sqlite3.connect(DB) as _c:   # création des tables (aussi sous gunicorn)
    _c.executescript(SCHEMA)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("FLASK_DEBUG") == "1")
