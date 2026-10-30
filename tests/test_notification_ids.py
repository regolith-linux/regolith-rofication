import json
import os
import tempfile
import unittest

from rofication._notification import Notification
from rofication._queue import NotificationQueue


def notification(application, summary='s', nid=0):
    ntf = Notification()
    ntf.id = nid
    ntf.application = application
    ntf.summary = summary
    ntf.body = ''
    return ntf


class NotificationIdTest(unittest.TestCase):
    """Notify() returns IDs that are never reused, also after the daemon restarts."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = os.path.join(self._dir.name, 'notifications.json')
        self.next_id_path = os.path.join(self._dir.name, 'notifications.next-id')

    def put(self, queue, application, summary='s', nid=0):
        ntf = notification(application, summary, nid)
        queue.put(ntf)
        return ntf.id

    def restart(self, queue):
        queue.save(self.path)
        return NotificationQueue.load(self.path)

    def test_ids_not_reused_after_restart(self):
        queue = NotificationQueue({}, single_notification_apps=())
        used = [self.put(queue, 'app', str(i)) for i in range(3)]
        queue.remove_all(used[1:])
        queue = self.restart(queue)
        self.assertNotIn(self.put(queue, 'app', 'after restart'), used)

    def test_ids_not_reused_after_restart_with_empty_queue(self):
        queue = NotificationQueue({}, single_notification_apps=())
        used = [self.put(queue, 'app', str(i)) for i in range(2)]
        queue.remove_all(used)
        queue = self.restart(queue)
        self.assertEqual(self.put(queue, 'app'), max(used) + 1)

    def test_stale_replaces_id_does_not_replace_another_notification(self):
        queue = NotificationQueue({}, single_notification_apps=())
        self.put(queue, 'other')
        old = self.put(queue, 'mail', 'held by the mail client')
        queue.remove(old)
        queue = self.restart(queue)
        self.put(queue, 'chat', 'new chat message')
        # the mail client still holds its old ID and updates "its" notification
        self.put(queue, 'mail', 'update', nid=old)
        self.assertEqual(sorted(n.application for n in queue), ['chat', 'mail', 'other'])

    def test_next_id_file(self):
        queue = NotificationQueue({}, single_notification_apps=())
        for i in range(4):
            self.put(queue, 'app', str(i))
        queue.save(self.path)
        with open(self.next_id_path) as fp:
            self.assertEqual(fp.read().strip(), '5')

    def test_queue_file_from_older_version(self):
        with open(self.path, 'w') as fp:
            json.dump([{'id': 7, 'application': 'app', 'summary': 'old', 'body': ''}], fp)
        queue = NotificationQueue.load(self.path)
        self.assertEqual(self.put(queue, 'app'), 8)

    def test_unreadable_next_id_file(self):
        with open(self.path, 'w') as fp:
            json.dump([{'id': 2, 'application': 'app', 'summary': 'old', 'body': ''}], fp)
        with open(self.next_id_path, 'w') as fp:
            fp.write('garbage')
        self.assertEqual(self.put(NotificationQueue.load(self.path), 'app'), 3)

    def test_queue_ids_above_next_id(self):
        with open(self.path, 'w') as fp:
            json.dump([{'id': 9, 'application': 'app', 'summary': 'old', 'body': ''}], fp)
        with open(self.next_id_path, 'w') as fp:
            fp.write('4\n')
        self.assertEqual(self.put(NotificationQueue.load(self.path), 'app'), 10)

    def test_no_files(self):
        self.assertEqual(self.put(NotificationQueue.load(self.path), 'app'), 1)


if __name__ == '__main__':
    unittest.main()
