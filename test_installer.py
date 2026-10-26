import argparse
import hashlib
import json
import pathlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import installer


class DiscoveryTests(unittest.TestCase):
    def test_sqlite_catalog_is_read_only_and_does_not_return_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            database=pathlib.Path(directory)/'panel.db'
            with sqlite3.connect(database) as connection:
                connection.executescript('CREATE TABLE settings(key TEXT,value TEXT); CREATE TABLE inbounds(id INTEGER,tag TEXT,remark TEXT,port INTEGER,protocol TEXT,settings TEXT);')
                connection.execute('INSERT INTO settings VALUES(?,?)',('webPort','2053'))
                connection.execute('INSERT INTO inbounds VALUES(1,?,?,?,?,?)',('inbound-443','Private <name>',443,'vless','{"private": "must not be exposed"}'))
            before=hashlib.sha256(database.read_bytes()).hexdigest()
            self.assertEqual(installer.Catalog('sqlite',database).settings()['webPort'],'2053')
            import server
            rows=server.Panel({'catalog_backend':'sqlite','panel_db':str(database)}).inbounds()
            self.assertEqual(set(rows[0]),{'id','tag','remark','port','protocol'})
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(),before)

    def test_postgres_environment_takes_precedence_over_old_sqlite_file(self):
        args=argparse.Namespace(backend='auto',panel_db=None,panel_unit='x-ui.service')
        with patch.object(installer,'panel_environment',return_value={'XUI_DB_TYPE':'postgres','XUI_DB_DSN':'host=127.0.0.1 port=5433 dbname=panel'}):
            catalog=installer.discover_catalog(args)
        self.assertEqual(catalog.backend,'postgres')
        self.assertEqual(catalog.options,{'host':'127.0.0.1','port':5433,'dbname':'panel'})

    def test_remote_postgres_fails_before_installation(self):
        args=argparse.Namespace(backend='postgres',panel_db=None,panel_unit='x-ui.service')
        with patch.object(installer,'panel_environment',return_value={'XUI_DB_DSN':'postgresql://user:secret@db.example.invalid/panel'}):
            with self.assertRaisesRegex(ValueError,'Remote/container'):installer.discover_catalog(args)

    def test_hostname_and_base_path_reject_url_injection(self):
        self.assertEqual(installer.detect_hostname({'webDomain':'panel.example.invalid'}),'panel.example.invalid')
        self.assertEqual(installer.normalize_base('/admin'),'/admin/')
        for value in ['https://panel.example.invalid','host/path','host\r\nInjected: value']:
            with self.assertRaises(ValueError):installer.detect_hostname({},value)
        with self.assertRaises(ValueError):installer.normalize_base('/path?bad')

    def test_uri_postgres_options_never_include_password(self):
        values=installer.pg_options('postgresql://admin:secret@127.0.0.1:5433/custom')
        self.assertEqual(values,{'host':'127.0.0.1','port':5433,'dbname':'custom'})
        self.assertNotIn('secret',json.dumps(values))


if __name__=='__main__':unittest.main(verbosity=2)
