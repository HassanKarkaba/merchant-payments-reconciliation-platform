"""Privileged local schema bootstrap, never imported by web or workers."""
import os
import psycopg
from psycopg import sql
for host,database,prefix in [('db','mprp','DB'),('simulator_db','simulator','SIM_DB')]:
    with psycopg.connect(host=host,dbname=database,user='postgres',password=os.environ[prefix+'_ADMIN_PASSWORD'],autocommit=True) as db:
        for role,password in [('mprp_owner',os.environ[prefix+'_OWNER_PASSWORD']),('mprp_runtime',os.environ[prefix+'_RUNTIME_PASSWORD']),('mprp_purge',os.environ[prefix+'_PURGE_PASSWORD'])]:
            if not db.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(role,)).fetchone():
                db.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE').format(sql.Identifier(role),sql.Literal(password)))
        db.execute('ALTER SCHEMA public OWNER TO mprp_owner')
        db.execute('GRANT USAGE ON SCHEMA public TO mprp_runtime')
        db.execute('ALTER DEFAULT PRIVILEGES FOR ROLE mprp_owner IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO mprp_runtime')
        db.execute('ALTER DEFAULT PRIVILEGES FOR ROLE mprp_owner IN SCHEMA public GRANT USAGE,SELECT ON SEQUENCES TO mprp_runtime')
print('Provisioned separate nonsuperuser migration/runtime roles.')
