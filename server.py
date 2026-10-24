"""Leeky local backend. Keep data/ and private-videos/ outside dist/."""
import hashlib, hmac, json, mimetypes, os, secrets, shutil, sqlite3, subprocess, threading, time
from decimal import Decimal, InvalidOperation
from watermark import watermarked
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, quote, unquote
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / 'dist'
DATA = Path(os.getenv('LEEKY_DATA_DIR', str(ROOT / 'data')))
PRIVATE = Path(os.getenv('LEEKY_PRIVATE_DIR', str(ROOT / 'private-videos')))
PRICE, CURRENCY = '14.99', 'USD'
API_KEY = os.getenv('RUNEPAY_API_KEY', '')
ORIGIN = (os.getenv('PUBLIC_URL') or os.getenv('RENDER_EXTERNAL_URL', '')).rstrip('/')
THUMBNAILS = DATA / 'thumbnails'
THUMB_LOCK = threading.Lock()
CHECKOUT_LOCK = threading.Lock()
CURRENCY_CACHE = {'until': 0, 'data': {}}
VIDEO_TYPES = {'.mp4', '.webm', '.mov', '.m4v', '.ogv', '.ogg'}

def thumbnail_id(path):
    stat=path.stat()
    return hashlib.sha256(f'start-end-v2:{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}'.encode()).hexdigest()

def capture_thumbnail(source):
    saved=THUMBNAILS/(thumbnail_id(source)+'.png')
    if saved.exists():return saved
    with THUMB_LOCK:
        if saved.exists():return saved
        THUMBNAILS.mkdir(parents=True,exist_ok=True)
        ffmpeg=shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'
        ffprobe=shutil.which('ffprobe') or str(Path(ffmpeg).with_name('ffprobe'))
        duration=float(subprocess.check_output([ffprobe,'-v','error','-show_entries','format=duration','-of','default=nw=1:nk=1',str(source)],timeout=30))
        start=duration*.05;end=duration*.92
        temporary=saved.with_suffix('.tmp.png')
        try:
            subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y','-ss',str(start),'-i',str(source),
              '-ss',str(end),'-i',str(source),'-filter_complex',
              '[0:v]scale=480:480,setsar=1[a];[1:v]scale=480:480,setsar=1[b];[a][b]hstack=inputs=2[out]',
              '-map','[out]','-frames:v','1',str(temporary)],capture_output=True,check=True,timeout=180)
            os.replace(temporary,saved)
            with (THUMBNAILS/'captures.jsonl').open('a') as log:
                log.write(json.dumps({'file':source.name,'thumbnail':saved.name,'captured_at':int(time.time()),
                  'duration':duration,'frames_seconds':[start,end],'unblurred':True})+'\n')
        finally:temporary.unlink(missing_ok=True)
    return saved

def video_files(folder):
    """Videos in folder and one level of subfolders, as (relative path, folder name, Path)."""
    found=[]
    if not folder.is_dir():return found
    for entry in sorted(folder.iterdir(),key=lambda p:p.name.lower()):
        if entry.name.startswith('.'):continue
        if entry.is_file() and entry.suffix.lower() in VIDEO_TYPES:found.append((entry.name,'',entry))
        elif entry.is_dir():
            for child in sorted(entry.iterdir(),key=lambda p:p.name.lower()):
                if not child.name.startswith('.') and child.is_file() and child.suffix.lower() in VIDEO_TYPES:
                    found.append((entry.name+'/'+child.name,entry.name,child))
    return found

def resolve_video(folder,relative):
    """Return the video at folder/relative, allowing one subfolder level, or None."""
    parts=relative.split('/')
    if not 1<=len(parts)<=2 or any(not part or part.startswith('.') for part in parts):return None
    target=(folder/relative).resolve();root=folder.resolve()
    if target.parent!=root and target.parent.parent!=root:return None
    if not target.is_file() or target.suffix.lower() not in VIDEO_TYPES:return None
    return target

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def db():
    con = sqlite3.connect(DATA / 'access.sqlite', timeout=20)
    con.row_factory = sqlite3.Row
    return con

def initialize():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    PRIVATE.mkdir(parents=True, exist_ok=True)
    (PUBLIC / 'videos').mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS settings(name TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, owner TEXT NOT NULL,
          track TEXT UNIQUE, checkout TEXT, status TEXT NOT NULL, amount TEXT NOT NULL,
          currency TEXT NOT NULL, key_hash TEXT UNIQUE, checked INTEGER DEFAULT 0,
          created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS limits(bucket TEXT PRIMARY KEY,count INTEGER,until INTEGER);
        CREATE TABLE IF NOT EXISTS views(video TEXT PRIMARY KEY,count INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS seen(video TEXT,visitor TEXT,until INTEGER,PRIMARY KEY(video,visitor));
        CREATE TABLE IF NOT EXISTS votes(video TEXT,visitor TEXT,vote INTEGER,PRIMARY KEY(video,visitor));
        CREATE TABLE IF NOT EXISTS visitors(id TEXT PRIMARY KEY,first INTEGER NOT NULL,last INTEGER NOT NULL,came_back INTEGER NOT NULL DEFAULT 0);
        ''')
        columns={row[1] for row in con.execute('PRAGMA table_info(orders)')}
        if 'invoice' not in columns:con.execute("ALTER TABLE orders ADD COLUMN invoice TEXT")
        con.execute('INSERT OR IGNORE INTO settings VALUES (?,?)', ('master', secrets.token_hex(32)))
    os.chmod(DATA / 'access.sqlite', 0o600)

def access_key(order):
    with db() as con:
        master=con.execute("SELECT value FROM settings WHERE name='master'").fetchone()[0]
    return 'LK-' + hmac.new(bytes.fromhex(master), order.encode(), hashlib.sha256).hexdigest().upper()

def is_sandbox(payment):
    return payment.get('sandbox') in (True,1,'1','true','True')

def fulfil(payment):
    """Only provider-authenticated records can call this function."""
    if not isinstance(payment, dict) or payment.get('status') != 'Paid':
        return False
    # White-label payments have no sandbox mode, and Rune Pay's payment details and
    # callbacks do not include the field. Refuse anything explicitly marked as a test.
    if is_sandbox(payment):
        return False
    with db() as con:
        row=con.execute('SELECT * FROM orders WHERE id=? AND track=?',
                        (payment.get('order_id'),payment.get('track_id'))).fetchone()
        if not row:return False
        try:
            amount=Decimal(str(payment.get('amount')))
            if not amount.is_finite() or amount != Decimal(row['amount']):return False
        except InvalidOperation:return False
        if payment.get('currency') != row['currency']:return False
        con.execute("UPDATE orders SET status='Paid',key_hash=? WHERE id=?",
                    (digest(access_key(row['id'])),row['id']))
    return True

def record_payment(payment):
    if not isinstance(payment,dict):return False
    if payment.get('status')=='Paid':return fulfil(payment)
    if is_sandbox(payment):return False
    if payment.get('status') not in ('Waiting','Underpaid','Confirming','Expired','Cancelled','Failed'):return False
    with db() as con:
        row=con.execute('SELECT * FROM orders WHERE id=? AND track=?',(payment.get('order_id'),payment.get('track_id'))).fetchone()
        if not row or payment.get('currency')!=row['currency']:return False
        try:
            amount=Decimal(str(payment.get('amount')))
            if not amount.is_finite() or amount!=Decimal(row['amount']):return False
        except InvalidOperation:return False
        con.execute("UPDATE orders SET status=? WHERE id=? AND status!='Paid'",(payment['status'],row['id']))
    return True

def supported_coins():
    if CURRENCY_CACHE['until']>time.time():return CURRENCY_CACHE['data']
    coins=rpay('/common/currencies');status=rpay('/common/status')
    active=status.get('networks',{})
    result={}
    for symbol,coin in coins.items():
        if not isinstance(coin,dict) or coin.get('status') is not True:continue
        networks=[{'id':key,'name':network.get('network',key)} for key,network in coin.get('networks',{}).items()
            if network.get('status')=='active' and active.get(key,{}).get('operational') is True]
        if networks:result[symbol]={'name':coin.get('name',symbol),'networks':networks}
    CURRENCY_CACHE.update(until=time.time()+60,data=result)
    return result

def invoice_details(invoice):
    # Only these provider fields are returned to the receipt owner.
    fields=('track_id','address','pay_amount','pay_currency','network','network_id','memo','expired_at','payment_url','qr_code')
    result={key:invoice.get(key) for key in fields}
    for field in ('payment_url','qr_code'):
        value=str(result[field] or '')
        url=urlparse(value)
        host=(url.hostname or '').lower()
        trusted=url.scheme=='https' and (host=='rpay.gg' or host.endswith('.rpay.gg'))
        if field=='qr_code' and value.startswith('data:image/'):trusted=True
        if not trusted:result[field]=None  # the page falls back to the copyable address
    if not all(result.get(key) for key in ('track_id','address','pay_amount','pay_currency','network_id')):raise ValueError('Incomplete invoice')
    amount=Decimal(str(result['pay_amount']))
    if not amount.is_finite() or amount<=0:raise ValueError('Invalid amount')
    if int(result['expired_at'])<=time.time():raise ValueError('Expired invoice')
    return result

class NoRedirect(__import__('urllib.request', fromlist=['HTTPRedirectHandler']).HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def rpay(path, body=None):
    from urllib.request import build_opener
    req=Request('https://rpay.gg/api/v1'+path,
                data=None if body is None else json.dumps(body).encode(),
                headers={'merchant_api_key':API_KEY,'Content-Type':'application/json'})
    with build_opener(NoRedirect).open(req,timeout=20) as response:
        result=json.load(response)
    if result.get('error') is not None:raise ValueError('Payment provider unavailable.')
    data=result.get('data')
    if isinstance(data,list):data=data[0] if len(data)==1 else None
    if not isinstance(data,dict):raise ValueError('Invalid provider response.')
    return data

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(PUBLIC),**kwargs)
    def log_message(self,*args):pass  # Never log access keys, cookies, or provider credentials.
    def end_headers(self):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('X-Frame-Options','DENY')
        super().end_headers()
    def reply(self,status,data,cookie=None):
        body=json.dumps(data).encode();self.send_response(status)
        self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store')
        if cookie:self.send_header('Set-Cookie',cookie)
        self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def cookies(self):
        try:
            c=SimpleCookie();c.load(self.headers.get('Cookie',''));return c
        except Exception:return SimpleCookie()
    def cookie(self,name):
        item=self.cookies().get(name);return item.value if item else ''
    def set_cookie(self,name,value,age):
        secure='; Secure' if ORIGIN.startswith('https://') else ''
        return f'{name}={value}; HttpOnly; SameSite=Lax; Path=/; Max-Age={age}{secure}'
    def authorized(self):
        with db() as con:
            return con.execute('SELECT 1 FROM sessions WHERE token=? AND expires>?',
                               (digest(self.cookie('lk_session')),int(time.time()))).fetchone() is not None
    def rate(self,kind,maximum):
        now=int(time.time());bucket=kind+':'+self.visitor_ip()
        with db() as con:
            con.execute('DELETE FROM limits WHERE until<?',(now,))
            row=con.execute('SELECT count FROM limits WHERE bucket=?',(bucket,)).fetchone()
            if row and row[0]>=maximum:return False
            con.execute('INSERT INTO limits VALUES (?,?,?) ON CONFLICT(bucket) DO UPDATE SET count=count+1',
                        (bucket,1,now+600))
        return True
    def visitor_ip(self):
        # Render's proxy puts the real visitor first in X-Forwarded-For.
        forwarded=self.headers.get('X-Forwarded-For','').split(',')[0].strip()
        return forwarded or self.client_address[0]
    def same_origin(self):
        origin=self.headers.get('Origin','')
        expected=ORIGIN or 'http://'+self.headers.get('Host','')
        return origin==expected
    def do_GET(self):
        path=unquote(urlparse(self.path).path)
        if path=='/healthz':
            self.reply(200,{'ok':True});return
        if path=='/api/currencies':
            try:self.reply(200,{'currencies':supported_coins()})
            except Exception:self.reply(503,{'error':'Payment networks unavailable. Try again shortly.'})
            return
        if path=='/api/config':
            count=len(video_files(PRIVATE))
            self.reply(200,{'price':PRICE,'currency':CURRENCY,'lifetime':True,'video_count':count,
                'ready':API_KEY.startswith('rp_sec_') and ORIGIN.startswith('https://'),
                'authenticated':self.authorized()});return
        if path=='/api/videos':
            paid=self.authorized();folder=PRIVATE if paid else PUBLIC/'videos'
            visitor=digest(self.cookie('lk_visitor')) if self.cookie('lk_visitor') else ''
            files=[]
            with db() as con:
                for name,group,p in video_files(folder):
                    views=con.execute('SELECT count FROM views WHERE video=?',(name,)).fetchone()
                    likes=con.execute('SELECT COUNT(*) FROM votes WHERE video=? AND vote=1',(name,)).fetchone()[0]
                    mine=con.execute('SELECT vote FROM votes WHERE video=? AND visitor=?',(name,visitor)).fetchone()
                    files.append({'title':p.stem,'folder':group,'src':('/media/' if paid else '/videos/')+quote(name),
                        'thumbnail':'/thumbnails/'+thumbnail_id(p)+'.png',
                        'views':views[0] if views else 0,'likes':likes,
                        'vote':{1:'like',-1:'dislike'}.get(mine[0] if mine else 0,'')})
            # Every subfolder shows, even before it has clips in it.
            folders=sorted({entry.name for entry in folder.iterdir() if entry.is_dir() and not entry.name.startswith('.')} if folder.is_dir() else set(),key=str.lower)
            locked=0 if paid else len(video_files(PRIVATE))
            self.reply(200,{'videos':files,'folders':folders,'full_access':paid,'locked':locked});return
        if path.startswith('/thumbnails/'):
            name=path[len('/thumbnails/'):]
            if len(name)!=68 or not name.endswith('.png') or any(c not in '0123456789abcdef' for c in name[:-4]):
                self.send_error(404);return
            source=None;paid=False
            for folder,is_paid in [(PUBLIC/'videos',False),(PRIVATE,True)]:
                for _,_,candidate in video_files(folder):
                    if thumbnail_id(candidate)==name[:-4]:
                        source=candidate;paid=is_paid;break
                if source:break
            if source is None:self.send_error(404);return
            if paid and not self.authorized():self.reply(401,{'error':'Enter an access key.'});return
            try:target=capture_thumbnail(source)
            except Exception:self.reply(503,{'error':'Could not create thumbnail.'});return
            image=target.read_bytes();self.send_response(200)
            self.send_header('Content-Type','image/png');self.send_header('Content-Length',str(len(image)))
            self.send_header('Cache-Control','private, max-age=86400');self.end_headers()
            if self.command!='HEAD':self.wfile.write(image)
            return
        if path.startswith('/media/') or path.startswith('/videos/'):
            paid=path.startswith('/media/')
            if paid and not self.authorized():self.reply(401,{'error':'Enter an access key.'});return
            folder=PRIVATE if paid else PUBLIC/'videos'
            prefix='/media/' if paid else '/videos/'
            target=resolve_video(folder,unquote(path[len(prefix):]))
            if target is None:
                self.send_error(404);return
            try:processed=watermarked(target,DATA/'watermarked',preview=not paid)
            except Exception:
                self.reply(503,{'error':'Video preview is being prepared or could not be processed. Please try again.'});return
            self.send_video(processed);return
        if path.startswith('/api/'):
            self.reply(404,{'error':'Not found.'});return
        # Static documents can never expose backend data or paid files.
        target=Path(self.translate_path(self.path)).resolve()
        if not target.is_relative_to(PUBLIC.resolve()):self.send_error(404);return
        relative=target.relative_to(PUBLIC.resolve())
        if (relative.parts and relative.parts[0]=='videos') or target.suffix.lower() in VIDEO_TYPES:
            self.send_error(404);return
        if target.is_dir() and not (target/'index.html').is_file():self.send_error(404);return
        super().do_GET()
    def do_HEAD(self):
        self.do_GET()
    def send_video(self,path):
        size=path.stat().st_size;start,end=0,size-1;partial=False
        value=self.headers.get('Range','')
        if value:
            try:
                if not value.startswith('bytes=') or ',' in value:raise ValueError()
                left,right=value[6:].split('-',1)
                if left:start=int(left);end=min(int(right),end) if right else end
                else:start=max(0,size-int(right))
                if start<0 or start>end or start>=size:raise ValueError()
                partial=True
            except ValueError:
                self.send_response(416);self.send_header('Content-Range',f'bytes */{size}');self.end_headers();return
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type',mimetypes.guess_type(path)[0] or 'application/octet-stream')
        self.send_header('Content-Length',str(end-start+1));self.send_header('Accept-Ranges','bytes')
        self.send_header('Cache-Control','private, no-store')
        if partial:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        if self.command=='HEAD':return
        with path.open('rb') as video:
            video.seek(start);remaining=end-start+1
            while remaining:
                chunk=video.read(min(65536,remaining))
                if not chunk:break
                self.wfile.write(chunk);remaining-=len(chunk)
    def do_POST(self):
        try:self.post()
        except (BrokenPipeError,ConnectionResetError):pass
        except (ValueError,TypeError):self.reply(400,{'error':'Invalid request.'})
        except Exception:self.reply(502,{'error':'Could not complete the request. Please try again.'})
    def post(self):
        path=unquote(urlparse(self.path).path)
        length=int(self.headers.get('Content-Length','0'))
        if length<0 or length>16384:self.reply(413,{'error':'Request too large.'});return
        raw=self.rfile.read(length)
        if path=='/api/rpay/callback':
            if not API_KEY:self.reply(503,{'error':'Not configured.'});return
            secret=hashlib.sha256(API_KEY.encode()).hexdigest().encode()
            signature=hmac.new(secret,raw,hashlib.sha512).hexdigest()
            if not hmac.compare_digest(signature,self.headers.get('HMAC','').lower()):
                self.reply(401,{'error':'Invalid signature.'});return
            event=json.loads(raw)
            if not isinstance(event,dict):self.reply(400,{'error':'Invalid event.'});return
            if event.get('type') in ('invoice','white_label'):
                if not record_payment(event):self.reply(422,{'error':'Payment details do not match.'});return
            self.reply(200,{'ok':True});return
        if not self.same_origin():self.reply(403,{'error':'Invalid origin.'});return
        if not self.rate('requests',120):self.reply(429,{'error':'Please try again later.'});return
        body=json.loads(raw or '{}')
        if not isinstance(body,dict):self.reply(400,{'error':'Invalid request.'});return
        if path=='/api/hello':
            # A returning user is a browser seen again in a new visit, 30+ minutes after its last one.
            visitor=self.cookie('lk_visitor') or secrets.token_urlsafe(24);who=digest(visitor);now=int(time.time())
            with db() as con:
                row=con.execute('SELECT last FROM visitors WHERE id=?',(who,)).fetchone()
                if not row:con.execute('INSERT INTO visitors VALUES (?,?,?,0)',(who,now,now))
                else:
                    if now-row[0]>=1800:con.execute('UPDATE visitors SET came_back=1 WHERE id=?',(who,))
                    con.execute('UPDATE visitors SET last=? WHERE id=?',(now,who))
                count=con.execute('SELECT COUNT(*) FROM visitors WHERE came_back=1').fetchone()[0]
            self.reply(200,{'returning':count},self.set_cookie('lk_visitor',visitor,31536000));return
        if path in ('/api/view','/api/vote'):
            source=str(body.get('src',''))
            prefix,folder=('/media/',PRIVATE) if source.startswith('/media/') else ('/videos/',PUBLIC/'videos')
            name=unquote(source[len(prefix):]) if source.startswith(prefix) else ''
            if not name or resolve_video(folder,name) is None:
                self.reply(404,{'error':'Video not found.'});return
            visitor=self.cookie('lk_visitor') or secrets.token_urlsafe(24);who=digest(visitor)
            cookie=self.set_cookie('lk_visitor',visitor,31536000)
            now=int(time.time())
            with db() as con:
                if path=='/api/view':
                    # One view per visitor per video every 6 hours.
                    seen=con.execute('SELECT until FROM seen WHERE video=? AND visitor=?',(name,who)).fetchone()
                    if not seen or seen[0]<now:
                        con.execute('INSERT INTO seen VALUES (?,?,?) ON CONFLICT(video,visitor) DO UPDATE SET until=excluded.until',(name,who,now+21600))
                        con.execute('INSERT INTO views VALUES (?,1) ON CONFLICT(video) DO UPDATE SET count=count+1',(name,))
                else:
                    vote={'like':1,'dislike':-1}.get(body.get('vote'),0)
                    if vote:con.execute('INSERT INTO votes VALUES (?,?,?) ON CONFLICT(video,visitor) DO UPDATE SET vote=excluded.vote',(name,who,vote))
                    else:con.execute('DELETE FROM votes WHERE video=? AND visitor=?',(name,who))
                views=con.execute('SELECT count FROM views WHERE video=?',(name,)).fetchone()
                likes=con.execute('SELECT COUNT(*) FROM votes WHERE video=? AND vote=1',(name,)).fetchone()[0]
            self.reply(200,{'views':views[0] if views else 0,'likes':likes},cookie);return
        if path=='/api/login':
            if not self.rate('login',15):self.reply(429,{'error':'Too many attempts. Try again later.'});return
            key=str(body.get('key','')).strip()
            with db() as con:
                row=con.execute("SELECT id FROM orders WHERE key_hash=? AND status='Paid'",(digest(key),)).fetchone()
                if not row:self.reply(401,{'error':'That key is not valid.'});return
                token=secrets.token_urlsafe(32)
                con.execute('INSERT INTO sessions VALUES (?,?)',(digest(token),int(time.time())+2592000))
            self.reply(200,{'ok':True},self.set_cookie('lk_session',token,2592000));return
        if path=='/api/checkout':
            # Serialize invoice creation so double-clicks cannot create two payments.
            with CHECKOUT_LOCK:self.create_checkout(body)
            return
        if path=='/api/cancel-checkout':
            # Cancels this browser's open invoice so the buyer can pick another coin.
            # A payment already on its way (Confirming/Underpaid) cannot be cancelled here.
            # If crypto still arrives for a cancelled invoice, the signed callback still marks it Paid.
            with db() as con:
                row=con.execute('SELECT * FROM orders WHERE owner=? ORDER BY created DESC LIMIT 1',(digest(self.cookie('lk_order')),)).fetchone()
                if not row:self.reply(404,{'error':'No checkout found in this browser.'});return
                if row['status'] in ('Paid','Confirming','Underpaid'):
                    self.reply(409,{'error':'A payment is already on its way, so this checkout can no longer be cancelled.'});return
                con.execute("UPDATE orders SET status='Cancelled' WHERE id=? AND status NOT IN ('Paid','Confirming','Underpaid')",(row['id'],))
            self.reply(200,{'status':'Cancelled'});return
        if path=='/api/payment-status':
            with db() as con:
                row=con.execute("SELECT * FROM orders WHERE owner=? ORDER BY status='Paid' DESC, created DESC LIMIT 1",
                                (digest(self.cookie('lk_order')),)).fetchone()
                if not row:self.reply(404,{'error':'No checkout found in this browser.'});return
                should_check=row['track'] and row['status']!='Paid' and row['checked']<time.time()-120
                if should_check:con.execute('UPDATE orders SET checked=? WHERE id=?',(int(time.time()),row['id']))
            if should_check:
                payment=rpay('/payment/'+quote(row['track'],safe=''))
                if payment.get('track_id',row['track'])!=row['track']:
                    self.reply(502,{'error':'Payment details could not be verified. Contact support with your receipt.'});return
                payment=dict(payment,track_id=row['track'],order_id=payment.get('order_id') or row['id'])
                if not record_payment(payment):
                    self.reply(502,{'error':'Payment details could not be verified. Contact support with your receipt.'});return
            with db() as con:row=con.execute('SELECT * FROM orders WHERE id=?',(row['id'],)).fetchone()
            result={'status':row['status']}
            if row['invoice']:result['invoice']=json.loads(row['invoice'])
            if row['status']=='Paid':result['key']=access_key(row['id'])
            self.reply(200,result);return
        self.reply(404,{'error':'Not found.'})

    def create_checkout(self,body):
        if not API_KEY.startswith('rp_sec_') or not ORIGIN.startswith('https://'):
            self.reply(503,{'error':'Payments are not configured yet.'});return
        owner=self.cookie('lk_order') or secrets.token_urlsafe(32)
        with db() as con:
            row=con.execute('SELECT * FROM orders WHERE owner=? ORDER BY created DESC LIMIT 1',(digest(owner),)).fetchone()
            if row and row['status'] in ('creating','uncertain'):
                self.reply(409,{'error':'Your previous checkout needs review. Contact support before paying again.'});return
            if row and row['status']=='Paid':
                self.reply(200,{'status':'Paid','key':access_key(row['id'])},self.set_cookie('lk_order',owner,2592000));return
            if row and row['invoice']:
                invoice=json.loads(row['invoice'])
                # Do not let users replace an outstanding invoice with another coin.
                if row['status'] not in ('Cancelled','Failed','Expired') and invoice['expired_at']>time.time():
                    self.reply(200,{'status':row['status'],'invoice':invoice},self.set_cookie('lk_order',owner,2592000));return
        coin=body.get('coin');network=body.get('network')
        coins=supported_coins()
        if coin not in coins or network not in [item['id'] for item in coins[coin]['networks']]:
            self.reply(400,{'error':'Choose an available coin and network.'});return
        if not self.rate('checkout',6):self.reply(429,{'error':'Please try again later.'});return
        order=secrets.token_urlsafe(24)
        with db() as con:
            con.execute('INSERT INTO orders(id,owner,status,amount,currency,created) VALUES (?,?,?,?,?,?)',
                (order,digest(owner),'creating',PRICE,CURRENCY,int(time.time())))
        try:
            payment=rpay('/payment/white-label',{'amount':float(PRICE),'currency':CURRENCY,'order_id':order,
                'pay_currency':coin,'network':network,'description':'Leeky — full video library, lifetime access',
                'under_paid_coverage':0,'lifetime':60,'callback_url':ORIGIN+'/api/rpay/callback'})
            invoice=invoice_details(payment)
            if invoice['pay_currency']!=coin or invoice['network_id']!=network:raise ValueError('Wrong payment route')
            with db() as con:
                con.execute('UPDATE orders SET track=?,checkout=?,status=?,invoice=? WHERE id=?',
                    (invoice['track_id'],invoice['payment_url'],'Waiting',json.dumps(invoice),order))
        except HTTPError as error:
            if error.code<500:
                with db() as con:con.execute("UPDATE orders SET status='Failed' WHERE id=?",(order,))
                self.reply(502,{'error':'Could not start checkout. Pick another coin or try again.'},self.set_cookie('lk_order',owner,2592000));return
            with db() as con:con.execute("UPDATE orders SET status='uncertain' WHERE id=?",(order,))
            self.reply(502,{'error':'Could not confirm checkout. Contact support before retrying.'},self.set_cookie('lk_order',owner,2592000));return
        except Exception:
            with db() as con:con.execute("UPDATE orders SET status='uncertain' WHERE id=?",(order,))
            self.reply(502,{'error':'Could not confirm checkout. Contact support before retrying.'},self.set_cookie('lk_order',owner,2592000));return
        self.reply(200,{'status':'Waiting','invoice':invoice},self.set_cookie('lk_order',owner,2592000))

if __name__=='__main__':
    import argparse,getpass
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=int(os.getenv('PORT','8767')));parser.add_argument('--host',default=os.getenv('HOST','127.0.0.1'));parser.add_argument('--ask-key',action='store_true')
    args=parser.parse_args()
    if args.ask_key:API_KEY=getpass.getpass('Rune Pay merchant key (hidden): ')
    initialize();print(f'Listening on {args.host}:{args.port}',flush=True)
    ThreadingHTTPServer((args.host,args.port),Handler).serve_forever()
