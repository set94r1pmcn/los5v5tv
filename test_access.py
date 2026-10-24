import importlib.util, tempfile, unittest, json, time, threading, urllib.request, urllib.error, hmac, hashlib
from unittest.mock import patch
from pathlib import Path
spec=importlib.util.spec_from_file_location('backend',Path(__file__).with_name('server.py'))
app=importlib.util.module_from_spec(spec);spec.loader.exec_module(app)

class AccessTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();app.DATA=Path(self.temp.name);app.PRIVATE=app.DATA/'private';app.ORIGIN='';app.initialize()
  with app.db() as con:con.execute("INSERT INTO orders(id,owner,track,status,amount,currency,created) VALUES (?,?,?,?,?,?,?)",('order',app.digest('receipt'),'invoice','Waiting','14.99','USD',int(time.time())))
  self.payment={'order_id':'order','track_id':'invoice','status':'Paid','sandbox':False,'amount':'14.990000','currency':'USD'}
 def tearDown(self):self.temp.cleanup()
 def test_payment_validation_and_duplicates(self):
  for change in ({'status':'Confirming'},{'sandbox':True},{'amount':'1'},{'currency':'EUR'},{'track_id':'wrong'},{'amount':'NaN'}):
   self.assertFalse(app.fulfil(dict(self.payment,**change)))
  self.assertTrue(app.fulfil(self.payment));key=app.access_key('order')
  self.assertTrue(app.fulfil(self.payment));self.assertEqual(key,app.access_key('order'))
  with app.db() as con:self.assertEqual(con.execute('SELECT key_hash FROM orders').fetchone()[0],app.digest(key))
 def test_checkout_provider_contract(self):
  app.API_KEY='rp_sec_unit_test_only';app.ORIGIN='https://leeky.example'
  server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
  thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:
   with patch.object(app,'supported_coins',return_value={'USDT':{'networks':[{'id':'TRC20'}]}}), patch.object(app,'rpay',return_value={'track_id':'INV_test','payment_url':'https://rpay.gg/checkout/INV_test','qr_code':'https://rpay.gg/checkout/INV_test/qr','address':'TEST_ONLY','pay_amount':'14.99','pay_currency':'USDT','network_id':'TRC20','expired_at':int(time.time())+3600}) as provider:
    request=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+'/api/checkout',data=b'{"coin":"USDT","network":"TRC20"}',headers={'Origin':app.ORIGIN,'Content-Type':'application/json'})
    with urllib.request.urlopen(request) as response:
     self.assertEqual(json.load(response)['invoice']['payment_url'],'https://rpay.gg/checkout/INV_test')
     self.assertIn('Secure',response.headers['Set-Cookie'])
     cookie=response.headers['Set-Cookie'].split(';')[0]
    path,body=provider.call_args.args
    self.assertEqual(path,'/payment/white-label')
    self.assertEqual(body['amount'],14.99);self.assertNotIn('sandbox',body)
    self.assertEqual(body['callback_url'],app.ORIGIN+'/api/rpay/callback')
    request.add_header('Cookie',cookie)
    with urllib.request.urlopen(request) as response:self.assertEqual(response.status,200)
    self.assertEqual(provider.call_count,1)
  finally:server.shutdown();server.server_close();thread.join();app.ORIGIN=''
 def test_http_login_callback_and_private_access(self):
  app.API_KEY='rp_sec_unit_test_only'
  server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
  thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  origin='http://127.0.0.1:'+str(server.server_port)
  def post(path,data,headers=None):
   request=urllib.request.Request(origin+path,data=json.dumps(data).encode(),headers={'Origin':origin,'Content-Type':'application/json',**(headers or {})})
   return urllib.request.urlopen(request)
  try:
   with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(origin+'/media/example.mp4')
   self.assertEqual(error.exception.code,401)
   with self.assertRaises(urllib.error.HTTPError):post('/api/login',{'key':'wrong'})
   with self.assertRaises(urllib.error.HTTPError):post('/api/rpay/callback',self.payment,{'HMAC':'forged'})
   event=dict(self.payment,type='invoice');raw=json.dumps(event).encode()
   secret=hashlib.sha256(app.API_KEY.encode()).hexdigest().encode()
   signature=hmac.new(secret,raw,hashlib.sha512).hexdigest()
   self.assertEqual(post('/api/rpay/callback',event,{'HMAC':signature}).status,200)
   with post('/api/login',{'key':app.access_key('order')}) as response:cookie=response.headers['Set-Cookie'].split(';')[0]
   req=urllib.request.Request(origin+'/api/videos',headers={'Cookie':cookie})
   self.assertTrue(json.load(urllib.request.urlopen(req))['full_access'])
   with post('/api/payment-status',{}, {'Cookie':'lk_order=receipt'}) as response:self.assertEqual(json.load(response)['key'],app.access_key('order'))
  finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
