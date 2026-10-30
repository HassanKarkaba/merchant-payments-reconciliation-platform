"""Real PostgreSQL row-lock barriers exercise resend/consume interleavings."""
import re
import threading
import time
import uuid
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.db import connections,connection
from django.test import TransactionTestCase,RequestFactory
from django.core.exceptions import ValidationError
from .models import User,AuthToken
from .domain import issue_token,consume_token,token_hash,decrypt
from .views import verify
from operations.models import Outbox

class TokenConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.user=User.objects.create_user(username='token-race',email='token-race@example.test',password='Fictional token race phrase 4791')
    def raw(self,token):
        for out in Outbox.objects.filter(recipient=self.user.email):
            match=re.search(r'/accounts/(?:verify|reset)/([^/]+)/',decrypt(out.encrypted_body))
            if match and token_hash(match.group(1))==token.token_hash:return match.group(1)
        raise AssertionError('No encrypted delivery copy corresponds to this token.')
    def verify_request(self,raw):
        request=RequestFactory().post('/accounts/verify/'+raw+'/',{})
        request.user=AnonymousUser();request.session={};request._messages=FallbackStorage(request)
        return verify(request,raw)
    def race(self,first,second,hold_sql):
        """Pause A after its protected SQL; observe B actually waiting on User."""
        boundary=threading.Barrier(2);release=threading.Event();held=threading.Event()
        results={};errors=[];pids={};once=False
        def hold(execute,sql,params,many,context):
            nonlocal once
            result=execute(sql,params,many,context)
            if not once and hold_sql(sql.upper()):
                once=True;held.set();boundary.wait(timeout=5)
                if not release.wait(timeout=5):raise AssertionError('Controller did not release the protected token boundary.')
            return result
        def worker(name,action):
            db=connections['default']
            try:
                db.ensure_connection();pids[name]=db.connection.info.backend_pid
                if name=='first':
                    with db.execute_wrapper(hold):results[name]=action()
                else:
                    boundary.wait(timeout=5);results[name]=action()
            except BaseException as error:errors.append(error)
            finally:db.close()
        a=threading.Thread(target=worker,args=('first',first),daemon=True)
        b=threading.Thread(target=worker,args=('second',second),daemon=True)
        a.start();b.start()
        observed=False
        try:
            self.assertTrue(held.wait(timeout=5),'First client never reached the controlled SQL boundary.')
            deadline=time.monotonic()+1.5
            while time.monotonic()<deadline:
                if 'second' in pids:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT wait_event_type,query FROM pg_stat_activity WHERE pid=%s',[pids['second']]);row=cursor.fetchone()
                    if row and row[0]=='Lock' and 'accounts_user' in row[1] and 'FOR UPDATE' in row[1].upper():observed=True;break
                # Poll an actual lock state; the test does not assume elapsed time
                # alone means that either request reached a protected boundary.
                time.sleep(.005)
            self.assertTrue(observed,'Second client did not wait on the common account lock.')
        finally:
            release.set();a.join(timeout=8);b.join(timeout=8)
        self.assertFalse(a.is_alive() or b.is_alive(),'A token request remained blocked after release.')
        if errors:raise errors[0]
        return results
    def test_two_simultaneous_resends_leave_only_latest_verification_or_reset_token(self):
        for kind in ('verify','reset'):
            with self.subTest(kind=kind):
                old=issue_token(kind,self.user.email,self.user)
                result=self.race(lambda:issue_token(kind,self.user.email,self.user),lambda:issue_token(kind,self.user.email,self.user),lambda sql:sql.startswith('UPDATE "ACCOUNTS_AUTHTOKEN"'))
                newest=result['second'];earlier=result['first']
                active=list(AuthToken.objects.filter(kind=kind,email=self.user.email,used_at=None).values_list('pk',flat=True))
                self.assertEqual(active,[newest.pk]);old.refresh_from_db();earlier.refresh_from_db()
                self.assertIsNotNone(old.used_at);self.assertIsNotNone(earlier.used_at)
                with self.assertRaises(ValidationError):consume_token(self.raw(earlier),kind)
                self.assertEqual(consume_token(self.raw(newest),kind).pk,newest.pk)
    def test_verify_and_resend_serialize_in_both_orders_without_deadlock(self):
        old=issue_token('verify',self.user.email,self.user);oldraw=self.raw(old)
        result=self.race(lambda:issue_token('verify',self.user.email,self.user),lambda:self.verify_request(oldraw),lambda sql:sql.startswith('UPDATE "ACCOUNTS_AUTHTOKEN"'))
        self.user.refresh_from_db();self.assertFalse(self.user.verified)
        self.assertEqual(list(AuthToken.objects.filter(kind='verify',email=self.user.email,used_at=None).values_list('pk',flat=True)),[result['first'].pk])
        current=result['first'];currentraw=self.raw(current)
        result=self.race(lambda:self.verify_request(currentraw),lambda:issue_token('verify',self.user.email,self.user),lambda sql:'FROM "ACCOUNTS_USER"' in sql and 'FOR UPDATE' in sql)
        self.user.refresh_from_db();self.assertTrue(self.user.verified)
        self.assertEqual(result['first'].status_code,302)
        self.assertEqual(list(AuthToken.objects.filter(kind='verify',email=self.user.email,used_at=None).values_list('pk',flat=True)),[result['second'].pk])
