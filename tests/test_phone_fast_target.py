import unittest

from assistant_mesh.phone_fast_target import candidates


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
