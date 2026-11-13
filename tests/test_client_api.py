from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from plagiarism.access import AccessError
from plagiarism.client_api import APIKeys,require_scope


class APIKeyTests(unittest.TestCase):
    def setUp(self):
        self.directory=TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.path=Path(self.directory.name)/'keys.sqlite3';self.clock=1000.
        self.persisted=0
        def persist():self.persisted+=1
        self.keys=APIKeys(self.path,persist,now=lambda:self.clock)
        self.alice='a'*64;self.bob='b'*64

    def test_one_time_secret_hashed_at_rest_and_scopes_survive_restart(self):
        issued=self.keys.create(self.alice,'My integration',['reports:read','quota:read'])
        self.assertTrue(issued['shown_once'])
        self.assertNotIn(issued['token'],repr(self.keys.list(self.alice)))
        with sqlite3.connect(self.path) as db:
            row=db.execute('select digest,scopes from api_keys').fetchone()
        self.assertNotIn(issued['token'],repr(row));self.assertEqual(len(row[0]),64)
        recovered=APIKeys(self.path,now=lambda:self.clock).authenticate('Bearer '+issued['token'])
        self.assertEqual(recovered['guest'],self.alice)
        require_scope(recovered,'reports:read')
        with self.assertRaises(AccessError):require_scope(recovered,'checks:write')

    def test_expiry_revocation_and_foreign_key_management(self):
        issued=self.keys.create(self.alice,'Short',['reports:read'],days=1)
        self.assertEqual(self.keys.list(self.bob),[])
        with self.assertRaises(KeyError):self.keys.revoke(self.bob,issued['key']['id'])
        self.keys.authenticate('Bearer '+issued['token'])
        self.clock+=86400
        with self.assertRaises(AccessError):self.keys.authenticate('Bearer '+issued['token'])
        issued=self.keys.create(self.alice,'Next',['reports:read'])
        self.keys.revoke(self.alice,issued['key']['id'])
        self.keys.revoke(self.alice,issued['key']['id'])
        with self.assertRaises(AccessError):self.keys.authenticate('Bearer '+issued['token'])

    def test_rate_limit_has_retry_after_and_minute_reset_does_not_spend_search_slots(self):
        issued=self.keys.create(self.alice,'Reader',['reports:read'])
        for _ in range(120):self.keys.authenticate('Bearer '+issued['token'])
        with self.assertRaises(AccessError) as caught:self.keys.authenticate('Bearer '+issued['token'])
        self.assertEqual(caught.exception.status,429);self.assertGreater(caught.exception.retry_after,0)
        self.clock+=60
        self.keys.authenticate('Bearer '+issued['token'])
        self.assertEqual(self.persisted,3,'Creation and at most one volume commit per used minute')

    def test_active_key_cap_and_invalid_inputs(self):
        issued=[self.keys.create(self.alice,'Key',['reports:read']) for _ in range(3)]
        with self.assertRaises(AccessError):self.keys.create(self.alice,'Fourth',['reports:read'])
        self.keys.revoke(self.alice,issued[0]['key']['id'])
        self.keys.create(self.alice,'Replacement',['reports:read'])
        for scopes,days in [([],30),(['admin'],30),(['reports:read'],True),(['reports:read'],91)]:
            with self.assertRaises(ValueError):self.keys.create(self.bob,'Invalid',scopes,days)
        for value in [None,'','Bearer secret','Basic abc','Bearer istok_v1_'+'a'*32+'.'+'b'*43]:
            with self.assertRaises(AccessError):self.keys.authenticate(value)


if __name__=='__main__':unittest.main()
