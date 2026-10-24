import sys,io,json,time,tempfile,hmac,hashlib
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
import server as app
app.DATA=Path(tempfile.mkdtemp());app.PRIVATE=app.DATA/'videos';app.initialize()
app.API_KEY='rp_sec_test';app.ORIGIN='https://leeky.example'
def call(path,body,headers=None):
 h=object.__new__(app.Handler);raw=json.dumps(body).encode()
 h.path=path;h.headers={'Content-Length':str(len(raw)),'Origin':app.ORIGIN,**(headers or {})};h.rfile=io.BytesIO(raw);h.client_address=('127.0.0.1',123);h.command='POST'
 result=[];h.reply=lambda *args:result.append(args);h.post();return result[0]
fixture={'track_id':'INV_test','payment_url':'https://rpay.gg/checkout/INV_test','qr_code':'https://rpay.gg/checkout/INV_test/qr','address':'TEST_ONLY','pay_amount':'14.990000','pay_currency':'USDT','network':'TRON','network_id':'TRC20','expired_at':int(time.time())+3600,'memo':'123'}
with patch.object(app,'supported_coins',return_value={'USDT':{'networks':[{'id':'TRC20'}]}}), patch.object(app,'rpay',return_value=fixture) as provider:
 result=call('/api/checkout',{'coin':'USDT','network':'TRC20'});assert result[0]==200;assert result[1]['invoice']['qr_code']==fixture['qr_code'];assert result[1]['invoice']['memo']=='123'
 cookie=result[2].split(';')[0];assert 'Secure' in result[2]
 path,body=provider.call_args.args;assert path=='/payment/white-label' and 'sandbox' not in body and 'return_url' not in body and body['amount']==14.99
 assert body['callback_url']==app.ORIGIN+'/api/rpay/callback'
 assert call('/api/checkout',{}, {'Cookie':cookie})[0]==200;assert provider.call_count==1
 with app.db() as con:order=con.execute('SELECT id FROM orders').fetchone()[0]
event={'type':'white_label','order_id':order,'track_id':'INV_test','status':'Paid','sandbox':False,'amount':'14.99','currency':'USD'}
def signed(event):
 signature=hmac.new(hashlib.sha256(app.API_KEY.encode()).hexdigest().encode(),json.dumps(event).encode(),hashlib.sha512).hexdigest()
 return call('/api/rpay/callback',event,{'HMAC':signature})
assert call('/api/rpay/callback',event,{'HMAC':'forged'})[0]==401
assert signed(dict(event,sandbox=True))[0]==422
real={k:v for k,v in event.items() if k!='sandbox'}  # what Rune Pay actually sends
assert signed(dict(event,status='Confirming'))[0]==200
with app.db() as con:con.execute('UPDATE orders SET checked=?',(int(time.time()),))
assert call('/api/payment-status',{}, {'Cookie':cookie})[1]['status']=='Confirming'
assert signed(real)[0]==200;assert signed(real)[0]==200
assert call('/api/login',{'key':app.access_key(order)})[0]==200
assert call('/api/payment-status',{}, {'Cookie':cookie})[1]['key']==app.access_key(order)
print('PASS: checkout request, duplicate reuse, secure cookies, forged callback rejection, sandbox rejection, signed fulfilment, repeat callback, login, receipt lookup')

assert app.record_payment(dict(event,status='Waiting'))
with app.db() as con:assert con.execute('SELECT status FROM orders').fetchone()[0]=='Paid'
assert app.invoice_details(dict(fixture,qr_code='https://evil.example/qr'))['qr_code'] is None
assert app.invoice_details(dict(fixture,qr_code='https://cdn.rpay.gg/qr.png'))['qr_code']=='https://cdn.rpay.gg/qr.png'
for field,value in [('pay_amount','NaN'),('expired_at',1)]:
 try:app.invoice_details(dict(fixture,**{field:value}))
 except (ValueError,TypeError):pass
 else:raise AssertionError(field)
print('PASS: white-label details, memo, provider URL validation, expiry validation, state transitions, paid status is permanent')
