import tempfile
import unittest
from pathlib import Path

from assistant_mesh.claw_delivery import MAX_AUTO_RETRY_AGE, MAX_RENEWAL_AGE, select_send
from assistant_mesh.store import Store


class DirectClawDelivery(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.time = 2000
        self.store = Store(Path(tmp.name) / 'authority.sqlite', clock=lambda: self.time)
        self.store.set('owner_context', {'version': 'original', 'token': 'fixture-only'})

    def reject(self, identity):
        self.store.enqueue(identity, 'fixture notification')
        self.assertEqual(identity, self.store.next_send()['id'])
        self.store.finish_send(identity, 'rejected', {'code': 'business_-2'})

    def test_original_context_renewal_is_recoverable_only_by_its_worker(self):
        self.reject('original-notice')
        first = self.store.claim_renewal('laptop')
        self.assertIsNotNone(first)
        self.assertEqual(first, self.store.claim_renewal('laptop'))
        self.assertIsNone(self.store.claim_renewal('different'))
        self.time += MAX_RENEWAL_AGE + 1
        self.assertIsNone(self.store.claim_renewal('laptop'))

    def test_old_direct_rejection_does_not_flush_on_new_context(self):
        self.reject('older')
        self.time += MAX_AUTO_RETRY_AGE + 1
        self.reject('current')
        self.store.set('owner_context', {'version': 'fresh', 'token': 'fixture-only'})
        with self.store.transaction() as db:
            row = select_send(db, {'version': 'fresh'}, self.time)
            self.assertEqual('current', row['id'])
        row = self.store.next_send()
        self.assertEqual('current', row['id'])
        self.store.finish_send('current', 'accepted')
        self.assertIsNone(self.store.next_send())
        self.assertEqual('rejected', self.store.send_status('older')['status'])
