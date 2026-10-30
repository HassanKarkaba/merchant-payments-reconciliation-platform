"""Redact bearer secrets from HTTP access logging before formatting."""
import logging
import re


SECRET_PATH = re.compile(r'(/(?:checkout|accounts/(?:verify|reset|invitations))/)(?!result/)([^/?\s\"\']+)')


class RedactSecretPaths(logging.Filter):
    def filter(self, record):
        # Freeze the formatted message, including Django's request-line args,
        # so another handler cannot recover the original bearer path.
        record.msg = re.sub(r'([?&]next=)[^&\s]+', r'\1[redacted]', SECRET_PATH.sub(r'\1[redacted]', record.getMessage()))
        record.args = ()
        return True
