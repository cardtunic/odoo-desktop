"""Keep report asset URLs on the desktop backend's current loopback port."""
import configparser
import sys
from urllib.parse import urlparse

import psycopg2


def configure(config_path, url):
    parsed = urlparse(url)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
            or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Invalid desktop report origin.')
    config = configparser.ConfigParser(interpolation=None)
    config.read(config_path)
    options = config['options']
    with psycopg2.connect(host=options.get('db_host', ''), port=options.getint('db_port', 5432),
            dbname=options['db_name'], user=options['db_user'], password=options.get('db_password', ''),
            sslmode=options.get('db_sslmode', 'prefer'), connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute("""INSERT INTO ir_config_parameter (key,value,create_uid,write_uid,create_date,write_date)
                VALUES ('report.url',%s,1,1,NOW(),NOW()) ON CONFLICT (key) DO UPDATE
                SET value=EXCLUDED.value,write_uid=1,write_date=NOW()""", (url,))


if __name__ == '__main__':
    try:
        configure(*sys.argv[1:])
    except Exception:
        print('Could not configure the report resource URL. Check the Odoo database connection and initialization.', file=sys.stderr)
        raise SystemExit(1)
