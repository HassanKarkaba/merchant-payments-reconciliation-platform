import os
import uuid
from pathlib import Path
from urllib.parse import urlparse
from django.core.exceptions import ImproperlyConfigured
BASE_DIR = Path(__file__).resolve().parent.parent

def secret(name):
    direct = os.environ.get(name)
    file = os.environ.get(name + '_FILE')
    if file:
        value = Path(file).read_text().strip()
        if direct and value != direct:
            raise ImproperlyConfigured(f'Conflicting {name} settings')
        return value
    if not direct or direct.startswith('REPLACE'):
        raise ImproperlyConfigured(f'{name} is required')
    return direct

SECRET_KEY = secret('DJANGO_SECRET_KEY')
FACTOR_ENCRYPTION_KEY = secret('FACTOR_ENCRYPTION_KEY')
from cryptography.fernet import Fernet
try:
    Fernet(FACTOR_ENCRYPTION_KEY.encode())
    MPRP_ENVIRONMENT_ID = str(uuid.UUID(os.environ['MPRP_ENVIRONMENT_ID']))
except (ValueError, KeyError) as exc:
    raise ImproperlyConfigured('Valid factor key and environment UUID required') from exc
MPRP_POLICY = 'mprp_v1'
if os.environ.get('MPRP_MODE', 'sandbox') != 'sandbox' or os.environ.get('MPRP_CURRENCY','USD') != 'USD':
    raise ImproperlyConfigured('Only the USD sandbox is supported')
DEBUG = False
if os.environ.get('DJANGO_DEBUG','0') != '0':
    raise ImproperlyConfigured('Sensitive sandbox flows require redacted errors; DEBUG must be 0')
ALLOWED_HOSTS = ['localhost','127.0.0.1','web','testserver']
ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'
INSTALLED_APPS = ['django.contrib.auth','django.contrib.contenttypes','django.contrib.sessions','django.contrib.messages','django.contrib.staticfiles','accounts','merchants','payments','accounting','settlements','reconciliation','disputes','operations']
MIDDLEWARE = ['operations.middleware.ProtectionMiddleware','django.middleware.security.SecurityMiddleware','django.contrib.sessions.middleware.SessionMiddleware','django.middleware.common.CommonMiddleware','django.middleware.csrf.CsrfViewMiddleware','django.contrib.auth.middleware.AuthenticationMiddleware','accounts.middleware.SessionAuthorityMiddleware','django.contrib.messages.middleware.MessageMiddleware','django.middleware.clickjacking.XFrameOptionsMiddleware']
TEMPLATES = [{'BACKEND':'django.template.backends.django.DjangoTemplates','DIRS':[BASE_DIR/'templates'],'APP_DIRS':True,'OPTIONS':{'context_processors':['django.template.context_processors.request','django.contrib.auth.context_processors.auth','django.contrib.messages.context_processors.messages','operations.context.navigation']}}]
u = urlparse(os.environ.get('DATABASE_URL', ''))
if u.scheme not in ('postgresql','postgres') or not u.hostname:
    raise ImproperlyConfigured('PostgreSQL DATABASE_URL required; SQLite is unsupported')
DATABASES = {'default':{'ENGINE':'django.db.backends.postgresql','NAME':u.path.lstrip('/'),'USER':u.username,'PASSWORD':u.password,'HOST':u.hostname,'PORT':u.port or 5432,'CONN_MAX_AGE':0,'OPTIONS':{'options':'-c lock_timeout=2000 -c statement_timeout=5000'}}}
AUTH_USER_MODEL = 'accounts.User'
AUTH_PASSWORD_VALIDATORS = [{'NAME':'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},{'NAME':'django.contrib.auth.password_validation.MinimumLengthValidator','OPTIONS':{'min_length':15}},{'NAME':'django.contrib.auth.password_validation.CommonPasswordValidator'},{'NAME':'django.contrib.auth.password_validation.NumericPasswordValidator'}]
AUTHENTICATION_BACKENDS = ['accounts.domain.EmailBackend']
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/'
SESSION_ENGINE = 'django.contrib.sessions.backends.db'
SESSION_COOKIE_AGE = 43200
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_SAVE_EVERY_REQUEST = True
CSRF_COOKIE_SAMESITE = 'Lax'
X_FRAME_OPTIONS = 'DENY'
SECURE_CONTENT_TYPE_NOSNIFF = True
USE_TZ = True
TIME_ZONE = 'UTC'
LANGUAGE_CODE = 'en-us'
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR/'collected-static'
STATICFILES_DIRS = [BASE_DIR/'static']
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
DATA_UPLOAD_MAX_MEMORY_SIZE = 6*1024*1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5*1024*1024
SIMULATOR_ORIGIN = os.environ.get('SIMULATOR_ORIGIN','http://simulator:8001').rstrip('/')
if SIMULATOR_ORIGIN not in ('http://simulator:8001','http://processor_sim:8001','http://127.0.0.1:8001'):
    raise ImproperlyConfigured('Unapproved simulator origin')
SIMULATOR_API_KEY = secret('SIMULATOR_API_KEY')
CALLBACK_HMAC_KEY = secret('CALLBACK_HMAC_KEY')
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.environ.get('EMAIL_HOST','mailpit')
if EMAIL_HOST != 'mailpit':
    raise ImproperlyConfigured('Only captured Mailpit email is supported')
EMAIL_PORT = 1025
EMAIL_TIMEOUT = 5
DEFAULT_FROM_EMAIL = 'sandbox@example.test'

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'filters': {'redact_secret_paths': {'()': 'operations.logging.RedactSecretPaths'}},
    'handlers': {'safe_console': {'class': 'logging.StreamHandler', 'filters': ['redact_secret_paths']}},
    'loggers': {'django.server': {'handlers': ['safe_console'], 'level': 'INFO', 'propagate': False}, 'django.request': {'handlers': ['safe_console'], 'level': 'WARNING', 'propagate': False}},
}

PUBLIC_ORIGIN = os.environ.get('PUBLIC_ORIGIN','http://localhost:8000').rstrip('/')
_public = urlparse(PUBLIC_ORIGIN)
if _public.scheme != 'http' or _public.hostname not in ('localhost','127.0.0.1') or _public.path or _public.username or _public.password:
    raise ImproperlyConfigured('Public sandbox origin must be local loopback HTTP')

FACTOR_ENCRYPTION_PREVIOUS_KEYS = [key for key in os.environ.get('FACTOR_ENCRYPTION_PREVIOUS_KEYS','').split(',') if key]
for _factor_key in FACTOR_ENCRYPTION_PREVIOUS_KEYS:
    try:Fernet(_factor_key.encode())
    except ValueError:raise ImproperlyConfigured('Invalid previous factor encryption key') from None
from .security import transport_keys
SIMULATOR_KEY_ID = os.environ.get('SIMULATOR_KEY_ID','local-v1')
CALLBACK_KEY_ID = os.environ.get('CALLBACK_KEY_ID','local-v1')
SIMULATOR_VERIFICATION_KEYS = transport_keys(SIMULATOR_KEY_ID,SIMULATOR_API_KEY,os.environ.get('SIMULATOR_PREVIOUS_KEYS','[]'))
CALLBACK_VERIFICATION_KEYS = transport_keys(CALLBACK_KEY_ID,CALLBACK_HMAC_KEY,os.environ.get('CALLBACK_PREVIOUS_KEYS','[]'))

if any(len(key)<32 for key in [SECRET_KEY,SIMULATOR_API_KEY,CALLBACK_HMAC_KEY]):
    raise ImproperlyConfigured('Application and transport secrets require at least 32 characters')
