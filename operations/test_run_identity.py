"""Actual separate processes cross the WSGI startup and HTTP cohort boundary."""
import json
import os
import select
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import quote
from unittest.mock import patch
from django.conf import settings
from django.db import connection,close_old_connections
from django.test import TransactionTestCase
from django.utils import timezone
from accounts.models import User
from merchants.models import Merchant
from operations.models import Control,AuditEvent,Notification,Outbox
from operations import run_identity


class ProcessRunIdentityTests(TransactionTestCase):
    def test_old_wsgi_process_cannot_adopt_new_epoch_or_send_then_fresh_process_can(self):
        control=Control.objects.create(environment_id=settings.MPRP_ENVIRONMENT_ID,simulator_instance_id=str(uuid.uuid4()),dispatch_epoch=3,restore_hold=False,hold_reason='')
        merchant=Merchant.objects.create(name='Cohort boundary fixture')
        received=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=self.rfile.read(int(self.headers.get('Content-Length','0')))
                received.append(json.loads(body))
                raw=b'{"ok":true}'
                self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        database=connection.settings_dict
        env=os.environ.copy()
        env['DATABASE_URL']='postgresql://'+quote(str(database['USER']),safe='')+':'+quote(str(database['PASSWORD']),safe='')+'@'+str(database['HOST'])+':'+str(database['PORT'] or 5432)+'/'+quote(str(database['NAME']),safe='')
        env['DJANGO_SETTINGS_MODULE']='config.settings'
        code='''
import json,sys,django
django.setup()
import config.wsgi
from django.conf import settings
from merchants.models import Merchant
from operations import provider,run_identity
settings.SIMULATOR_ORIGIN=sys.argv[2]
merchant=Merchant.objects.get(pk=sys.argv[1])
print('READY '+str(run_identity.is_bound()),flush=True)
sys.stdin.readline()
try:
    body=provider.envelope(merchant,'c'*32,{})
    provider.request('POST','/v1/cohort-observer/',body)
    print(json.dumps({'result':'SENT','epoch':body['dispatch_epoch']}),flush=True)
except provider.ProviderError:
    print(json.dumps({'result':'BLOCKED'}),flush=True)
'''
        processes=[]
        def start():
            process=subprocess.Popen([sys.executable,'-u','-c',code,str(merchant.pk),'http://127.0.0.1:'+str(server.server_port)],env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
            processes.append(process)
            raw=b'';deadline=time.monotonic()+30
            while b'READY True\n' not in raw and time.monotonic()<deadline:
                ready,_,_=select.select([process.stdout],[],[],max(0,deadline-time.monotonic()))
                if not ready:break
                part=os.read(process.stdout.fileno(),4096)
                if not part:break
                raw+=part
            self.assertIn(b'READY True\n',raw,'The actual WSGI entry must bind before serving requests')
            return process
        def release(process):
            process.stdin.write(b'go\n');process.stdin.flush()
            output,error=process.communicate(timeout=30)
            self.assertEqual(process.returncode,0,error.decode(errors='replace'))
            return json.loads(output.decode().strip().splitlines()[-1])
        try:
            old=start()
            Control.objects.filter(pk=1).update(run_token=uuid.uuid4(),dispatch_epoch=4)
            self.assertEqual(release(old),{'result':'BLOCKED'})
            self.assertEqual(received,[],'A replaced process must make no provider HTTP request')
            fresh=start()
            self.assertEqual(release(fresh),{'result':'SENT','epoch':4})
            self.assertEqual(len(received),1)
            self.assertEqual(received[0]['dispatch_epoch'],4)
        finally:
            for process in processes:
                if process.poll() is None:process.kill();process.communicate()
            server.shutdown();server.server_close();thread.join(timeout=2)


class ClockHoldAtomicityTests(TransactionTestCase):
    def test_four_workers_publish_one_atomic_critical_hold_notice(self):
        from operations.worker import tick
        instant=timezone.now()
        control=Control.objects.create(environment_id=settings.MPRP_ENVIRONMENT_ID,simulator_instance_id=str(uuid.uuid4()),dispatch_epoch=3,restore_hold=False,hold_reason='',provider_ready=True,provider_checked_at=instant,last_clock=instant+__import__('datetime').timedelta(seconds=3))
        operator=User.objects.create_user(username='clock_ops',email='clock@example.test',verified=True,totp_confirmed=True,platform_role='OPERATIONS')
        barrier=threading.Barrier(5)
        errors=[]
        def worker():
            close_old_connections()
            try:
                barrier.wait(timeout=10);tick(uuid.uuid4())
            except Exception as error:errors.append(error)
            finally:close_old_connections()
        with patch('operations.run_identity._bound_identity',None):
            run_identity.bind_process_identity(control)
            with patch('operations.worker.database_now',return_value=instant),patch('operations.worker.claim',return_value=None):
                threads=[threading.Thread(target=worker) for _ in range(4)]
                for thread in threads:thread.start()
                barrier.wait(timeout=10)
                for thread in threads:thread.join(timeout=15)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors,[])
        control.refresh_from_db()
        self.assertTrue(control.restore_hold)
        events=AuditEvent.objects.filter(action='recovery.hold')
        self.assertEqual(events.count(),1)
        self.assertNotEqual(events.get().resource_id,str(control.main_instance_id))
        self.assertEqual(Notification.objects.filter(recipient=operator,event_key__startswith='recovery.hold:').count(),1)
        self.assertEqual(Outbox.objects.filter(recipient=operator.email).count(),1)
