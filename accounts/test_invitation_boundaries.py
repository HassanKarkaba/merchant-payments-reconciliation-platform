"""Invitation authority cannot replace protected team/lifecycle transitions."""
import re
import uuid
from unittest.mock import patch
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.exceptions import PermissionDenied
from django.test import TransactionTestCase,RequestFactory
from django.utils import timezone
from accounts.models import User,AuthToken
from accounts.domain import issue_token
from accounts.views import invitation,deactivate
from merchants.models import Merchant,Membership
from merchants.views import invite,change_member
from operations.views import merchant_action

PASSWORD='Fictional account phrase 3298'
class InvitationBoundaryTests(TransactionTestCase):
    def setUp(self):
        self.owner=User.objects.create_user(username='invite-owner',email='invite-owner@example.test',password=PASSWORD,verified=True,totp_confirmed=True)
        self.recipient=User.objects.create_user(username='invite-recipient',email='invite-recipient@example.test',password=PASSWORD,verified=True,totp_confirmed=True)
        self.merchant=Merchant.objects.create(name='Fictional Invite Co',status='ACTIVE')
        self.owner_membership=Membership.objects.create(merchant=self.merchant,user=self.owner,role='OWNER')
    def request(self,path,data,user=None):
        request=RequestFactory().post(path,data)
        request.user=user or AnonymousUser();request.session={'auth_epoch':getattr(user,'auth_epoch',None),'step_up_at':__import__('time').time(),'step_up_reference':'test-invite-step-up'}
        request._messages=FallbackStorage(request)
        return request
    def token(self,email=None,role='FINANCE',issuer=None):
        with patch('operations.domain.mail') as mail:
            obj=issue_token('invite',email or self.recipient.email,user=issuer or self.owner,merchant=self.merchant,role=role)
        raw=re.search(r'/accounts/invitations/([^/]+)/',mail.call_args.args[2]).group(1)
        return obj,raw
    def accept(self,raw,password=PASSWORD):
        return invitation(self.request('/accounts/invitations/'+raw+'/',{'password':password}),raw)
    def test_self_invite_cannot_remove_last_owner_or_overwrite_active_role(self):
        request=self.request('/m/'+str(self.merchant.pk)+'/team/invite/',{'email':self.owner.email,'role':'FINANCE'},self.owner)
        with self.assertRaises(PermissionDenied):invite(request,self.merchant.pk)
        token,raw=self.token(email=self.owner.email,role='FINANCE')
        response=self.accept(raw)
        self.owner_membership.refresh_from_db();token.refresh_from_db()
        self.assertEqual(self.owner_membership.role,'OWNER');self.assertTrue(self.owner_membership.active);self.assertIsNone(token.used_at)
        self.assertContains(response,'already an active member')
    def test_closed_merchant_refuses_admission_and_pending_acceptance(self):
        token,raw=self.token()
        self.merchant.status='CLOSED';self.merchant.closed_at=timezone.now();self.merchant.save(update_fields=['status','closed_at'])
        request=self.request('/m/'+str(self.merchant.pk)+'/team/invite/',{'email':'fresh@example.test','role':'FINANCE'},self.owner)
        with self.assertRaises(PermissionDenied):invite(request,self.merchant.pk)
        self.assertContains(self.accept(raw),'Invitation unavailable')
        self.assertFalse(Membership.objects.filter(merchant=self.merchant,user=self.recipient).exists())
    def test_recipient_revocation_invalidates_old_token_new_owner_invite_can_reactivate(self):
        member=Membership.objects.create(merchant=self.merchant,user=self.recipient,role='ANALYST',active=False)
        old,raw=self.token()
        request=self.request('/team/change/',{'expected_version':member.version,'active':'0','role':'ANALYST'},self.owner)
        change_member(request,self.merchant.pk,member.pk)
        old.refresh_from_db();self.assertIsNotNone(old.used_at)
        self.assertContains(self.accept(raw),'Invitation unavailable')
        fresh,freshraw=self.token()
        response=self.accept(freshraw)
        member.refresh_from_db();self.assertTrue(member.active);self.assertEqual(member.role,'FINANCE');self.assertEqual(response.status_code,302)
    def test_issuer_revocation_and_reinstatement_cannot_revive_old_token(self):
        second=User.objects.create_user(username='invite-owner-two',email='invite-owner-two@example.test',password=PASSWORD,verified=True,totp_confirmed=True)
        Membership.objects.create(merchant=self.merchant,user=second,role='OWNER')
        token,raw=self.token()
        change_member(self.request('/team/change/',{'expected_version':self.owner_membership.version,'active':'1','role':'ANALYST'},second),self.merchant.pk,self.owner_membership.pk)
        self.owner_membership.refresh_from_db()
        change_member(self.request('/team/change/',{'expected_version':self.owner_membership.version,'active':'1','role':'OWNER'},second),self.merchant.pk,self.owner_membership.pk)
        token.refresh_from_db();self.assertIsNotNone(token.used_at)
        self.assertContains(self.accept(raw),'Invitation unavailable')
    def test_closure_invalidates_all_pending_merchant_invitations(self):
        token,raw=self.token()
        operator=User.objects.create_user(username='invite-ops',email='invite-ops@example.test',password=PASSWORD,verified=True,totp_confirmed=True,platform_role='OPERATIONS')
        self.merchant.status='CLOSING';self.merchant.save(update_fields=['status'])
        merchant_action(self.request('/operations/close/',{'reason':'Fictional zero-activity merchant closure'},operator),self.merchant.pk,'close')
        token.refresh_from_db();self.assertIsNotNone(token.used_at)
        self.assertContains(self.accept(raw),'Invitation unavailable')
