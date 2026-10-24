import sys,io,json,time,tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import server as app
app.DATA=Path(tempfile.mkdtemp());app.PRIVATE=app.DATA/'videos';app.THUMBNAILS=app.DATA/'thumbnails';app.initialize()
app.API_KEY='rp_sec_test';app.ORIGIN='https://leeky.example'
(app.PRIVATE/'clip one.mp4').write_bytes(b'x')

def call(path,body,headers=None,ip='10.0.0.1'):
 h=object.__new__(app.Handler);raw=json.dumps(body).encode()
 h.path=path;h.headers={'Content-Length':str(len(raw)),'Origin':app.ORIGIN,**(headers or {})};h.rfile=io.BytesIO(raw);h.client_address=(ip,1);h.command='POST'
 out=[];h.reply=lambda *a:out.append(a);h.post();return out[0]

# Rate limits are per visitor, not per Render proxy IP.
for _ in range(15):call('/api/login',{'key':'bad'},{'X-Forwarded-For':'1.1.1.1, 10.0.0.1'})
assert call('/api/login',{'key':'bad'},{'X-Forwarded-For':'1.1.1.1'})[0]==429
assert call('/api/login',{'key':'bad'},{'X-Forwarded-For':'2.2.2.2'})[0]==401

# Thumbnail uploads are gone.
assert call('/api/thumbnail',{'src':'/videos/a.mp4'},{'X-Forwarded-For':'3.3.3.3'})[0]==404

# Views count once per visitor; votes are real and toggle.
r=call('/api/view',{'src':'/media/clip%20one.mp4'},{'X-Forwarded-For':'4.4.4.4'});assert r[1]['views']==1
c=r[2].split(';')[0]
assert call('/api/view',{'src':'/media/clip%20one.mp4'},{'Cookie':c,'X-Forwarded-For':'4.4.4.4'})[1]['views']==1
assert call('/api/view',{'src':'/media/clip%20one.mp4'},{'X-Forwarded-For':'5.5.5.5'})[1]['views']==2
assert call('/api/vote',{'src':'/media/clip%20one.mp4','vote':'like'},{'Cookie':c,'X-Forwarded-For':'4.4.4.4'})[1]['likes']==1
assert call('/api/vote',{'src':'/media/clip%20one.mp4','vote':''},{'Cookie':c,'X-Forwarded-For':'4.4.4.4'})[1]['likes']==0
assert call('/api/view',{'src':'/media/../server.py'},{'X-Forwarded-For':'4.4.4.4'})[0]==404

# Retrieval responses without order_id still reconcile against the stored order.
with app.db() as con:con.execute("INSERT INTO orders(id,owner,track,status,amount,currency,created,checked) VALUES ('o1',?,'T1','Waiting','14.99','USD',?,0)",(app.digest('rc'),int(time.time())))
with patch.object(app,'rpay',return_value={'track_id':'T1','status':'Paid','sandbox':False,'amount':'14.99','currency':'USD'}):
 r=call('/api/payment-status',{},{'Cookie':'lk_order=rc','X-Forwarded-For':'6.6.6.6'})
assert r[1]['status']=='Paid' and r[1]['key']==app.access_key('o1')
print('PASS: per-visitor limits, no thumbnail uploads, real views/votes, path guard, retrieval without order_id')

# Folders: one level of subfolders is listed, served and counted; deeper paths are refused.
(app.PRIVATE/'Ranked').mkdir();(app.PRIVATE/'Ranked'/'ace.mp4').write_bytes(b'x')
(app.PRIVATE/'Ranked'/'deep').mkdir();(app.PRIVATE/'Ranked'/'deep'/'no.mp4').write_bytes(b'x')
assert [r for r,_,_ in app.video_files(app.PRIVATE)]==['clip one.mp4','Ranked/ace.mp4']
assert app.resolve_video(app.PRIVATE,'Ranked/ace.mp4') and not app.resolve_video(app.PRIVATE,'Ranked/deep/no.mp4') and not app.resolve_video(app.PRIVATE,'../x.mp4')
assert call('/api/view',{'src':'/media/Ranked/ace.mp4'},{'X-Forwarded-For':'7.7.7.7'})[1]['views']==1
print('PASS: folders listed, served and counted; deeper paths refused')

# Empty folders and folder names starting with @ work.
(app.PRIVATE/'@yera').mkdir()
assert app.resolve_video(app.PRIVATE,'@yera/x.mp4') is None
(app.PRIVATE/'@yera'/'x.mp4').write_bytes(b'x')
assert app.resolve_video(app.PRIVATE,'@yera/x.mp4')
print('PASS: @yera folder')

# Locked count tells visitors there is more behind access.
h=object.__new__(app.Handler);h.path='/api/videos';h.headers={};h.command='GET';h.client_address=('1.1.1.1',1)
out=[];h.reply=lambda *a:out.append(a);h.do_GET()
assert out[0][1]['locked']==len(app.video_files(app.PRIVATE))==3 and out[0][1]['full_access'] is False
print('PASS: locked count for view more')

# Returning users: counted only when the same browser comes back 30+ minutes later.
r=call('/api/hello',{},{'X-Forwarded-For':'8.8.8.8'});assert r[1]['returning']==0
c=r[2].split(';')[0]
assert call('/api/hello',{},{'Cookie':c,'X-Forwarded-For':'8.8.8.8'})[1]['returning']==0
with app.db() as con:con.execute('UPDATE visitors SET last=last-1900')
assert call('/api/hello',{},{'Cookie':c,'X-Forwarded-For':'8.8.8.8'})[1]['returning']==1
assert call('/api/hello',{},{'Cookie':c,'X-Forwarded-For':'8.8.8.8'})[1]['returning']==1
print('PASS: returning user count')

# Rune Pay's documented shapes: no sandbox field, amount as a number. These must unlock access.
import hmac,hashlib,json as _json
from urllib.error import HTTPError
with app.db() as con:con.execute("INSERT INTO orders(id,owner,track,status,amount,currency,created,checked) VALUES ('o2',?,'T2','Waiting','14.99','USD',?,0)",(app.digest('rc2'),int(time.time())))
doc_get={'type':'white_label','track_id':'T2','status':'Paid','amount':14.99,'currency':'USD','pay_amount':'14.990000','pay_currency':'USDT','network':'TRC20','order_id':'o2','txs':[]}
with patch.object(app,'rpay',return_value=doc_get):
 r=call('/api/payment-status',{},{'Cookie':'lk_order=rc2','X-Forwarded-For':'9.9.9.1'})
assert r[1]['status']=='Paid' and r[1]['key']==app.access_key('o2'),r
with app.db() as con:con.execute("INSERT INTO orders(id,owner,track,status,amount,currency,created) VALUES ('o3',?,'T3','Waiting','14.99','USD',?)",(app.digest('rc3'),int(time.time())))
raw=_json.dumps(dict(doc_get,track_id='T3',order_id='o3')).encode()
sig=hmac.new(hashlib.sha256(app.API_KEY.encode()).hexdigest().encode(),raw,hashlib.sha512).hexdigest()
h=object.__new__(app.Handler);h.path='/api/rpay/callback';h.headers={'Content-Length':str(len(raw)),'HMAC':sig};h.rfile=io.BytesIO(raw);h.client_address=('1.2.3.4',1);h.command='POST'
out=[];h.reply=lambda *a:out.append(a);h.post();assert out[0][0]==200,out
assert app.fulfil(dict(doc_get,sandbox=True)) is False
# A rejected create request (4xx) marks the order Failed so the buyer can try again.
def reject(*a,**k):raise HTTPError('https://rpay.gg/api/v1/payment/white-label',422,'Unprocessable',{},None)
with patch.object(app,'supported_coins',return_value={'USDT':{'networks':[{'id':'TRC20'}]}}),patch.object(app,'rpay',side_effect=reject):
 r=call('/api/checkout',{'coin':'USDT','network':'TRC20'},{'Cookie':'lk_order=newbuyer','X-Forwarded-For':'9.9.9.2'})
assert r[0]==502 and 'try again' in r[1]['error']
with app.db() as con:assert con.execute("SELECT status FROM orders WHERE owner=?",(app.digest('newbuyer'),)).fetchone()[0]=='Failed'
print('PASS: documented Rune Pay payloads unlock access; rejected checkout can be retried')

# Cancel: an open invoice can be cancelled; a payment on its way cannot; a late payment still unlocks.
with app.db() as con:con.execute("INSERT INTO orders(id,owner,track,status,amount,currency,created,invoice) VALUES ('c1',?,'TC1','Waiting','14.99','USD',?,?)",(app.digest('rcc'),int(time.time()),_json.dumps({'expired_at':int(time.time())+3600})))
r=call('/api/cancel-checkout',{},{'Cookie':'lk_order=rcc','X-Forwarded-For':'9.9.9.3'});assert r[0]==200
with app.db() as con:assert con.execute("SELECT status FROM orders WHERE id='c1'").fetchone()[0]=='Cancelled'
with app.db() as con:con.execute("UPDATE orders SET status='Confirming' WHERE id='c1'")
assert call('/api/cancel-checkout',{},{'Cookie':'lk_order=rcc','X-Forwarded-For':'9.9.9.3'})[0]==409
with app.db() as con:con.execute("UPDATE orders SET status='Cancelled' WHERE id='c1'")
assert app.record_payment({'type':'white_label','order_id':'c1','track_id':'TC1','status':'Paid','amount':14.99,'currency':'USD'})
with app.db() as con:con.execute("INSERT INTO orders(id,owner,track,status,amount,currency,created) VALUES ('c2',?,'TC2','Waiting','14.99','USD',?)",(app.digest('rcc'),int(time.time())+5))
with patch.object(app,'rpay',return_value={'track_id':'TC2','status':'Waiting','amount':14.99,'currency':'USD','order_id':'c2'}):
 r=call('/api/payment-status',{},{'Cookie':'lk_order=rcc','X-Forwarded-For':'9.9.9.3'})
assert r[1].get('key')==app.access_key('c1'),r
print('PASS: cancel checkout, no cancel once paying, late payment still unlocks')
