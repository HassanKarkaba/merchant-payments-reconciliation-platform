"""Restricted sandbox provider process; main feature apps and databases are excluded."""
import os
from pathlib import Path
from urllib.parse import urlparse, unquote
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', '')
if len(SECRET_KEY) < 32:
    raise ImproperlyConfigured('DJANGO_SECRET_KEY requires at least 32 characters')
DEBUG = False
ALLOWED_HOSTS = ['processor_sim', 'simulator', 'localhost', '127.0.0.1', 'testserver']
ROOT_URLCONF = 'processor_sim.urls'
INSTALLED_APPS = ['processor_sim.apps.ProcessorSimConfig']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware']
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
USE_TZ = True
TIME_ZONE = 'UTC'
SIMULATOR_API_KEY = os.environ.get('SIMULATOR_API_KEY', '')
SIMULATOR_KEY_ID = os.environ.get('SIMULATOR_KEY_ID', 'local-v1')
CALLBACK_HMAC_KEY = os.environ.get('CALLBACK_HMAC_KEY', os.environ.get('CALLBACK_API_KEY', ''))
CALLBACK_ORIGIN = os.environ.get('CALLBACK_ORIGIN', 'http://web:8000')
MPRP_ENVIRONMENT_ID = os.environ.get('MPRP_ENVIRONMENT_ID', '')
if len(SIMULATOR_API_KEY) < 32 or not MPRP_ENVIRONMENT_ID:
    raise ImproperlyConfigured('Simulator API key and explicit environment are required')
_dsn = os.environ.get('SIMULATOR_DATABASE_URL', os.environ.get('DATABASE_URL', ''))
_url = urlparse(_dsn)
if _url.scheme not in {'postgres', 'postgresql'}:
    raise ImproperlyConfigured('Simulator requires an independent PostgreSQL database URL')
DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql', 'NAME': unquote(_url.path.lstrip('/')), 'USER': unquote(_url.username or ''), 'PASSWORD': unquote(_url.password or ''), 'HOST': _url.hostname or '', 'PORT': _url.port or 5432, 'CONN_MAX_AGE': 0, 'OPTIONS': {'options': '-c lock_timeout=2000 -c statement_timeout=5000'}}}
DATA_UPLOAD_MAX_MEMORY_SIZE = 1048576
SECURE_CONTENT_TYPE_NOSNIFF = True

from .security import transport_keys
CALLBACK_KEY_ID = os.environ.get('CALLBACK_KEY_ID','local-v1')
SIMULATOR_VERIFICATION_KEYS = transport_keys(SIMULATOR_KEY_ID,SIMULATOR_API_KEY,os.environ.get('SIMULATOR_PREVIOUS_KEYS','[]'))
