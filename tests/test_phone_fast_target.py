import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock, patch

from assistant_mesh.phone_fast_target import candidates, locate
from tests.test_phone_control import blob


class FastPhoneTarget(unittest.TestCase):
    def test_normalized_coordinates_and_ambiguous_text_stay_tied_to_bounded_frames(self):
        tsv = '\n'.join((
            'level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext',
            '5\t1\t1\t1\t1\t1\t10\t20\t20\t12\t93\t设置',
            '5\t1\t1\t1\t2\t1\t60\t145\t24\t14\t80\t设',
            '5\t1\t1\t1\t2\t2\t84\t145\t11\t14\t79\t置',
            '5\t1\t1\t1\t3\t1\t96\t80\t12\t13\t99\t设置',
            '5\t1\t1\t1\t4\t1\t10\t190\t12\t5\t94\t无关内容',
        ))
        points, total = candidates(tsv, '设 置', width=100, height=200)
        self.assertEqual(2, total)
        self.assertEqual([(200000, 130000), (775000, 760000)], [(p['x'], p['y']) for p in points])
        self.assertNotIn('无关内容', str(points))

    def test_locate_exports_reusable_frame_expectation_and_paired_coordinate_suggestion(self):
        lookup = {'task_id': 'task:phone', 'task_epoch': 4, 'operation_id': 'op:share',
                  'capability_id': 'test-phone:ui-control-v1'}
        image = blob(b'\xff\xd8\xffsynthetic screen\xff\xd9')
        client = Mock()
        client.request.return_value = dict(
            lookup, format='amita-phone-screen-share-frame/1', stream_id='stream:original',
            binding_digest='a' * 64, frame=dict(image, revision=3, capture_timestamp=123, width=100, height=200))
        tsv = ('level\tpage_num\tblock_num\tpar_num\tline_num\tleft\ttop\twidth\theight\tconf\ttext\n'
               '5\t1\t1\t1\t1\t25\t50\t50\t100\t90\tSettings\n')
        with patch('assistant_mesh.phone_fast_target.subprocess.run', return_value=CompletedProcess(
                args=[], returncode=0, stdout=tsv.encode())) as ocr:
            result = locate(client, lookup, 'settings')
        self.assertEqual(result['frame_expectation'], dict(
            lookup, stream_id='stream:original', binding_digest='a' * 64, revision=3,
            sha256=image['sha256'], width=100, height=200))
        self.assertEqual(result['candidates'][0]['paired_phone_coordinates'], {'x': 16384, 'y': 16384})
        self.assertFalse(result['action_executed'])
        self.assertFalse(Path(ocr.call_args.args[0][1]).exists())
        self.assertNotIn(image['base64'], str(result))
