import os
from django.core.wsgi import get_wsgi_application
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
application=get_wsgi_application()

# Explicit web-entry startup binding; model/app imports and migrations do not query it.
from operations.run_identity import bind_process_identity
bind_process_identity()
