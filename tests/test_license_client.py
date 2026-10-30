import base64
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import license_client as module


class LicenseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.key=Ed25519PrivateKey.generate()
        self.pub=patch.object(module,'PUBLIC_KEY',self.key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex())
        self.pub.start();self.addCleanup(self.pub.stop)
        self.client=module.LicenseClient(Path(self.temp.name)/'license.json','atlas','2053:/panel/',self.transport)
        self.revoked=False;self.calls=[]

    def transport(self,path,body):
        self.calls.append((path,body));stamp=int(time.time())
        payload={'product':body['product'],'license_id':'test-license','installation_id':body['installation_id'],
            'machine_hash':body['machine_hash'],'resource_id':body['resource_id'],'status':'revoked' if self.revoked else 'active',
            'issued_at':stamp,'lease_until':stamp+86400,'grace_until':stamp+72*3600,'expires_at':stamp+30*86400}
        envelope={'payload':payload,'signature':base64.b64encode(self.key.sign(module.canonical(payload))).decode()}
        return {'installation_token':'t'*50,'lease':envelope} if path=='/activate' else envelope

    def test_activation_stores_token_but_never_key_and_status_hides_token(self):
        self.assertFalse(self.client.status()['can_manage'])
        key='FS-'+'x'*40;self.assertTrue(self.client.activate(key)['can_manage'])
        self.assertNotIn(key,self.client.path.read_text())
        self.assertNotIn('installation_token',json.dumps(self.client.status()))
        self.client.require_management()

    def test_cross_product_machine_and_panel_copied_state_rejected(self):
        self.client.activate('FS-'+'x'*40)
        for attribute,value in [('product','zarib'),('machine','f'*64),('resource','e'*64)]:
            original=getattr(self.client,attribute);setattr(self.client,attribute,value)
            self.assertEqual(self.client.status()['status'],'invalid');setattr(self.client,attribute,original)

    def test_lost_activation_response_retries_with_same_installation_id(self):
        def offline(path,body):raise OSError('network unavailable')
        self.client.transport=offline
        with self.assertRaises(OSError):self.client.activate('FS-'+'x'*40)
        install=self.client.read()['installation_id'];self.client.transport=self.transport
        self.client.activate('FS-'+'x'*40)
        self.assertEqual(self.calls[0][1]['installation_id'],install)

    def test_signed_grace_is_bounded_and_issuer_unavailable_does_not_extend_it(self):
        self.client.activate('FS-'+'x'*40);stamp=int(time.time())
        self.client.transport=lambda *a: (_ for _ in ()).throw(OSError())
        self.assertTrue(self.client.refresh()['can_manage'])
        with patch.object(module.time,'time',return_value=stamp+25*3600):
            self.assertTrue(self.client.status()['offline_grace'])
        with patch.object(module.time,'time',return_value=stamp+73*3600):
            self.assertFalse(self.client.status()['can_manage'])
            with self.assertRaises(module.LicenseError):self.client.require_management()

    def test_revocation_remains_revoked_after_expiry(self):
        self.client.activate('FS-'+'x'*40);self.revoked=True
        self.assertEqual(self.client.refresh()['status'],'revoked')
        with patch.object(module.time,'time',return_value=int(time.time())+40*86400):
            self.assertEqual(self.client.status()['status'],'revoked')

    def test_tampering_or_corrupt_state_fails_closed(self):
        self.client.activate('FS-'+'x'*40);data=self.client.read()
        data['lease']['payload']['expires_at']+=365*86400;self.client.save(data)
        self.assertEqual(self.client.status()['status'],'invalid')
        self.client.path.write_text('{')
        self.assertEqual(self.client.status()['status'],'invalid')


if __name__=='__main__':unittest.main()
