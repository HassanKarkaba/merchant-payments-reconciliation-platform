"""Generate an isolated local sandbox configuration without loading Django."""
import argparse
import base64
import os
from pathlib import Path
import secrets
import uuid
parser=argparse.ArgumentParser();parser.add_argument('--output',default='.local');parser.add_argument('--project',default='mprp_local');args=parser.parse_args()
if not args.project.replace('_','').isalnum() or args.project.lower()!=args.project:raise SystemExit('Use a lowercase isolated project identity')
folder=Path(args.output);folder.mkdir(parents=True,exist_ok=True)
path=folder/'environment'
if path.exists():raise SystemExit('Configuration already exists; refusing to overwrite')
values={'COMPOSE_PROJECT_NAME':args.project,'MPRP_ENVIRONMENT_ID':str(uuid.uuid4()),'DJANGO_SECRET_KEY':secrets.token_hex(48),'FACTOR_ENCRYPTION_KEY':base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),'SIMULATOR_API_KEY':secrets.token_hex(32),'CALLBACK_HMAC_KEY':secrets.token_hex(32),'DB_ADMIN_PASSWORD':secrets.token_hex(24),'DB_OWNER_PASSWORD':secrets.token_hex(24),'DB_RUNTIME_PASSWORD':secrets.token_hex(24),'DB_PURGE_PASSWORD':secrets.token_hex(24),'SIM_DB_ADMIN_PASSWORD':secrets.token_hex(24),'SIM_DB_OWNER_PASSWORD':secrets.token_hex(24),'SIM_DB_RUNTIME_PASSWORD':secrets.token_hex(24),'SIM_DB_PURGE_PASSWORD':secrets.token_hex(24),'WEB_PORT':'8000','MAIL_PORT':'8025'}
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as file:
    for key,value in values.items():file.write(key+'='+value+'\n')
print('Generated owner-readable local configuration for '+args.project)
