import time
import uuid
from unittest.mock import patch
from datetime import timedelta
import pyotp
from django.test import TestCase
from django.core.exceptions import ValidationError,PermissionDenied
from django.utils import timezone
from django.contrib.auth.hashers import make_password
from .models import User,AuthToken
from .domain import normalize_email,encrypt,decrypt,consume_totp,consume_recovery,consume_token,token_hash,require_step_up
from merchants.models import Merchant,Membership
class IdentityTests(TestCase):
    def setUp(self):
        self.secret=pyotp.random_base32();self.user=User.objects.create_user(username=str(uuid.uuid4()),email='owner@example.test',password='Synthetic passphrase for tests',verified=True,totp_secret=encrypt(self.secret),totp_confirmed=True)
        self.merchant=Merchant.objects.create(name='Fictional Store',status='ACTIVE');Membership.objects.create(user=self.user,merchant=self.merchant,role='OWNER')
    def test_email_scope_and_normalization(self):
        self.assertEqual(normalize_email(' OWNER+test@Example.Test '),'owner+test@example.test')
        for email in ['real@gmail.com','a@еxample.test','bad address@example.test']:
            with self.assertRaises(ValidationError):normalize_email(email)
    def test_factor_encrypted_and_counter_replay_denied(self):
        self.assertNotIn(self.secret,self.user.totp_secret);self.assertEqual(decrypt(self.user.totp_secret),self.secret)
        code=pyotp.TOTP(self.secret).now();self.assertTrue(consume_totp(self.user,code));self.assertFalse(consume_totp(self.user,code))
    def test_recovery_code_consumed_once(self):
        self.user.recovery_hashes=[make_password('one-use-recovery')];self.user.save()
        self.assertTrue(consume_recovery(self.user,'one-use-recovery'));self.assertFalse(consume_recovery(self.user,'one-use-recovery'))
    def test_token_single_use_and_expiry_equality(self):
        now=timezone.now();AuthToken.objects.create(token_hash=token_hash('raw-verify'),kind='verify',email=self.user.email,user=self.user,expires_at=now)
        with patch('accounts.domain.timezone.now',return_value=now):
            with self.assertRaises(ValidationError):consume_token('raw-verify','verify')
        AuthToken.objects.filter(token_hash=token_hash('raw-verify')).update(expires_at=now+timedelta(hours=1))
        consume_token('raw-verify','verify')
        with self.assertRaises(ValidationError):consume_token('raw-verify','verify')
    def session(self):
        self.client.force_login(self.user,backend='accounts.domain.EmailBackend');s=self.client.session;s.update({'auth_epoch':self.user.auth_epoch,'mfa_ok':True,'absolute_start':time.time(),'last_activity':time.time()});s.save()
    def test_epoch_and_idle_revoke_protected_session(self):
        self.session();self.assertEqual(self.client.get('/').status_code,302)
        User.objects.filter(pk=self.user.pk).update(auth_epoch=2)
        response=self.client.get('/m/'+str(self.merchant.pk)+'/');self.assertEqual(response.status_code,302);self.assertIn('/accounts/login/',response.url)
    def test_cross_tenant_known_id_is_404(self):
        other=Merchant.objects.create(name='Other business');self.session()
        self.assertEqual(self.client.get(f'/m/{other.pk}/').status_code,404)
    def test_analyst_cannot_create_link(self):
        Membership.objects.filter(user=self.user).update(role='ANALYST');self.session()
        self.assertEqual(self.client.get(f'/m/{self.merchant.pk}/links/new/').status_code,403)
    def test_public_get_does_not_create_financial_work(self):
        from operations.models import Operation
        before=Operation.objects.count();self.client.get('/accounts/register/');self.client.get('/accounts/login/');self.assertEqual(Operation.objects.count(),before)
    def test_csrf_blocks_public_registration(self):
        from django.test import Client
        client=Client(enforce_csrf_checks=True)
        self.assertEqual(client.post('/accounts/register/',{'email':'new@example.test'}).status_code,403)
    def test_redirect_and_security_headers(self):
        response=self.client.get('/accounts/login/?next=https://outside.example/')
        self.assertContains(response,'value="/"');self.assertEqual(response['Cache-Control'],'no-store');self.assertIn("frame-ancestors 'none'",response['Content-Security-Policy'])

class IdentityBoundaryRepairTests(IdentityTests):
    def test_expired_or_anonymous_polling_is_json_401(self):
        endpoint = f'/m/{self.merchant.pk}/operations/{uuid.uuid4()}/status.json'
        response = self.client.get(endpoint)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.session()
        User.objects.filter(pk=self.user.pk).update(auth_epoch=self.user.auth_epoch + 1)
        response = self.client.get(endpoint)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['code'], 'authentication_required')

    def test_enrollment_session_stores_only_encrypted_factor_secret(self):
        self.user.totp_confirmed = False
        self.user.save(update_fields=['totp_confirmed'])
        self.session()
        response = self.client.post('/accounts/mfa/enroll/', {'begin': '1', 'password': 'Synthetic passphrase for tests'})
        self.assertEqual(response.status_code, 200)
        shown = response.context['secret']
        retained = self.client.session['enrollment_secret']
        self.assertNotIn(shown, retained)
        self.assertEqual(decrypt(retained), shown)
        self.assertNotContains(self.client.get('/accounts/mfa/enroll/'), shown)

    def test_invitation_resend_invalidates_only_same_merchant(self):
        from .domain import issue_token
        other = Merchant.objects.create(name='Other fictional store')
        old = issue_token('invite', 'invite@example.test', merchant=self.merchant, role='FINANCE')
        unrelated = issue_token('invite', 'invite@example.test', merchant=other, role='ANALYST')
        replacement = issue_token('invite', 'invite@example.test', merchant=self.merchant, role='OWNER')
        old.refresh_from_db()
        unrelated.refresh_from_db()
        self.assertIsNotNone(old.used_at)
        self.assertIsNone(unrelated.used_at)
        self.assertIsNone(replacement.used_at)

    def test_unknown_account_form_fields_are_bad_transport(self):
        response = self.client.post('/accounts/login/', {'email': self.user.email, 'password': 'Synthetic passphrase for tests', 'merchant_id': str(self.merchant.pk)})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_successful_authentication_does_not_consume_failure_budget(self):
        for _ in range(6):
            response = self.client.post('/accounts/login/', {'email': self.user.email, 'password': 'Synthetic passphrase for tests'})
            self.assertEqual(response.status_code, 303)

    def test_login_and_mfa_share_failed_attempt_budget(self):
        for _ in range(3):
            self.assertEqual(self.client.post('/accounts/login/', {'email': self.user.email, 'password': 'wrong fictional password'}).status_code, 200)
        self.session()
        with patch('accounts.views.consume_totp', return_value=False), patch('accounts.views.consume_recovery', return_value=False):
            self.assertEqual(self.client.post('/accounts/mfa/', {'code': '000000'}).status_code, 200)
            self.assertEqual(self.client.post('/accounts/mfa/', {'code': '000000'}).status_code, 200)
            response = self.client.post('/accounts/mfa/', {'code': '000000'})
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response['Retry-After'], '60')

    def test_http_access_logging_redacts_bearer_paths(self):
        import logging
        from operations.logging import RedactSecretPaths
        filter = RedactSecretPaths()
        for path in ['/checkout/opaque-bearer-secret/', '/accounts/verify/opaque-bearer-secret/', '/accounts/reset/opaque-bearer-secret/', '/accounts/invitations/opaque-bearer-secret/']:
            record = logging.LogRecord('django.server', logging.INFO, '', 1, '"GET %s HTTP/1.1" 200', (path,), None)
            self.assertTrue(filter.filter(record))
            self.assertNotIn('opaque-bearer-secret', record.getMessage())

class KeyRotationTests(TestCase):
    def test_previous_factor_key_decrypts_and_new_encryption_uses_active(self):
        from cryptography.fernet import Fernet,InvalidToken
        from django.test import override_settings
        old=Fernet.generate_key();new=Fernet.generate_key();cipher=Fernet(old).encrypt(b'factor-value').decode()
        with override_settings(FACTOR_ENCRYPTION_KEY=new.decode(),FACTOR_ENCRYPTION_PREVIOUS_KEYS=[old.decode()]):
            self.assertEqual(decrypt(cipher),'factor-value');rotated=encrypt(decrypt(cipher))
            self.assertEqual(Fernet(new).decrypt(rotated.encode()),b'factor-value')
            with self.assertRaises(InvalidToken):Fernet(old).decrypt(rotated.encode())
    def test_previous_transport_key_has_bounded_explicit_window(self):
        import json
        from config.security import transport_keys,verification_secret
        now=timezone.now()
        value=[{'key_id':'old','secret':'x'*64,'activated_at':(now-timedelta(hours=1)).isoformat(),'expires_at':(now+timedelta(hours=1)).isoformat()}]
        keys=transport_keys('new','y'*64,json.dumps(value));self.assertEqual(verification_secret(keys,'old'),'x'*64)
        value[0]['expires_at']=(now-timedelta(seconds=1)).isoformat()
        keys=transport_keys('new','y'*64,json.dumps(value));self.assertIsNone(verification_secret(keys,'old'))
        value[0]['expires_at']=(now+timedelta(hours=25)).isoformat()
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaises(ImproperlyConfigured):transport_keys('new','y'*64,json.dumps(value))

class RedactedErrorTests(TestCase):
    def test_unhandled_login_error_does_not_echo_credentials(self):
        from django.db import DatabaseError
        from django.test import Client
        client=Client(raise_request_exception=False)
        password='Fictional credential never echoed 293!'
        with patch('accounts.views.authenticate',side_effect=DatabaseError('Database unavailable')):
            response=client.post('/accounts/login/',{'email':'fictional@example.test','password':password})
        self.assertEqual(response.status_code,500);self.assertNotIn(password.encode(),response.content)
