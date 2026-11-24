"""Find a text target on the latest authorized phone frame using local OCR."""
import argparse
import csv
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unicodedata

from .config import private_json
from .ingress import _identifier
from .paired_phone_input import ABSOLUTE_MAX
from .phone_frame_client import read_frame
from .worker import Client

COORDINATE_MAX = 1_000_000


def _normalized(text):
    return ''.join(unicodedata.normalize('NFKC', text).casefold().split())


def candidates(tsv, query, width, height):
    """Return exact text matches in frame coordinates. Never execute input."""
    needle = _normalized(query)
    if not needle or len(needle) > 96 or not (1 <= width <= 640 and 1 <= height <= 640):
        raise ValueError('phone_target_query_or_geometry_invalid')
    lines = {}
    for row in csv.DictReader(io.StringIO(tsv), delimiter='\t', quoting=csv.QUOTE_NONE):
        if row.get('level') != '5' or not row.get('text'):
            continue
        try:
            x, y, w, h = [int(row[k]) for k in ('left', 'top', 'width', 'height')]
            confidence = float(row['conf'])
            line = tuple(int(row[k]) for k in ('page_num', 'block_num', 'par_num', 'line_num'))
        except (KeyError, ValueError, TypeError):
            continue
        if (not 0 <= x < x + w <= width or not 0 <= y < y + h <= height
                or not math.isfinite(confidence) or confidence < 0 or confidence > 100):
            continue
        word = _normalized(row['text'])
        if word:
            lines.setdefault(line, []).append((word, x, y, x + w, y + h, confidence))

    found = []
    for words in lines.values():
        for start in range(len(words)):
            phrase = ''
            for end in range(start, min(start + 12, len(words))):
                phrase += words[end][0]
                if not needle.startswith(phrase):
                    break
                if phrase != needle:
                    continue
                matching = words[start:end + 1]
                bounds = [min(word[1] for word in matching), min(word[2] for word in matching),
                          max(word[3] for word in matching), max(word[4] for word in matching)]
                x, y = (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2
                found.append({'x': round(x * COORDINATE_MAX / width),
                              'y': round(y * COORDINATE_MAX / height),
                              'bounds_px': bounds, 'confidence': round(min(word[5] for word in matching), 2)})
                break
    found.sort(key=lambda row: (-row['confidence'], row['y'], row['x']))
    return found[:8], len(found)


def locate(client, lookup, query, language='chi_sim+eng'):
    if language not in ('chi_sim+eng', 'chi_sim', 'eng'):
        raise ValueError('phone_target_ocr_language_invalid')
    with tempfile.TemporaryDirectory(prefix='amita-phone-target-') as temporary:
        frame = read_frame(client, lookup, Path(temporary).resolve())
        result = subprocess.run(
            ['tesseract', frame['path'], 'stdout', '-l', language, '--psm', '11', 'tsv'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=6)
        if result.returncode != 0 or len(result.stdout) > 256 * 1024:
            raise ValueError('phone_target_ocr_unavailable')
        matches, count = candidates(result.stdout.decode('utf-8'), query, frame['width'], frame['height'])
    state = 'unique_candidate' if count == 1 else 'ambiguous' if count else 'not_found'
    for match in matches:
        match['paired_phone_coordinates'] = {
            axis: round(match[axis] * ABSOLUTE_MAX / COORDINATE_MAX) for axis in ('x', 'y')}
    return {'format': 'amita-phone-target/1', 'frame_revision': frame['revision'],
            'frame_sha256': frame['sha256'], 'width': frame['width'], 'height': frame['height'],
            'frame_expectation': frame['frame_expectation'],
            'state': state, 'candidates': matches, 'matches': count, 'source': 'local_ocr',
            'action_executed': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--task-epoch', required=True, type=int)
    parser.add_argument('--operation-id', required=True)
    parser.add_argument('--capability-id', required=True)
    parser.add_argument('--query', required=True)
    parser.add_argument('--language', default='chi_sim+eng', choices=('chi_sim+eng', 'chi_sim', 'eng'))
    args = parser.parse_args(argv)
    if args.task_epoch <= 0:
        parser.error('task epoch must be positive')
    lookup = {'task_id': args.task_id, 'task_epoch': args.task_epoch,
              'operation_id': args.operation_id, 'capability_id': args.capability_id}
    try:
        for key in ('task_id', 'operation_id', 'capability_id'):
            _identifier(lookup[key], 'phone_target_' + key)
        print(json.dumps(locate(Client(private_json(args.config)), lookup, args.query, args.language)))
    except (ValueError, OSError, subprocess.SubprocessError):
        print('No confirmed phone target for the original task and current frame.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
