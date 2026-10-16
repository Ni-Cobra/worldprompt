"""Lazy, bounded spatial evidence over a private immutable capture."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import tempfile

import numpy as np

from .block_state import parse_state
from .core import BlockVolume, apply_plan, coord
from .limits import MAX_BLOCKS, MAX_INSPECT_BLOCKS, MAX_PREVIEWS, MAX_REGION_BYTES, MAX_SURFACE_COLUMNS, MAX_SURFACE_STEP


def write_snapshot(volume, directory):
    """Keep raw blocks out of the model's initial context and tool arguments."""
    directory = Path(directory)
    np.save(directory / 'source_blocks.npy', volume.blocks, allow_pickle=False)
    (directory / 'source_palette.json').write_text(json.dumps(volume.palette), encoding='utf-8')


class SpatialTools:
    def __init__(self, directory, dimensions, max_operations, lecterns=()):
        self.directory = Path(directory) if directory is not None else None
        self.lecterns = [list(position) for position in lecterns]
        self.dimensions = tuple(dimensions)
        self.max_operations = max_operations
        self.source = None
        self.candidate = None
        self.preview_calls = 0

    def load_source(self):
        if self.source is None:
            if self.directory is None or not (self.directory / 'source_blocks.npy').is_file():
                raise ValueError('Exact source capture unavailable for this planner request')
            # Fixed local names only; callers cannot supply paths or pickle data.
            blocks = np.load(self.directory / 'source_blocks.npy', mmap_mode='r', allow_pickle=False)
            palette = json.loads((self.directory / 'source_palette.json').read_text(encoding='utf-8'))
            w, h, length = self.dimensions
            if blocks.shape != (h, length, w) or blocks.dtype != np.uint32 or blocks.size > MAX_BLOCKS:
                raise ValueError('Invalid private source snapshot')
            if not isinstance(palette, list) or not palette or int(blocks.max()) >= len(palette):
                raise ValueError('Invalid private source palette')
            self.source = BlockVolume(blocks, palette)
            self.source.blocks.flags.writeable = False
        return self.source

    def inspect_region(self, min, max, target='source'):
        low = coord(min, self.dimensions, 'min')
        high = coord(max, self.dimensions, 'max')
        if any(a > b for a, b in zip(low, high)):
            raise ValueError('min exceeds max')
        if int(np.prod([b - a + 1 for a, b in zip(low, high)])) > MAX_INSPECT_BLOCKS:
            raise ValueError(f'Inspect at most {MAX_INSPECT_BLOCKS} blocks per call; split large regions or columns')
        volume = self._target(target)
        x0, y0, z0 = low
        x1, y1, z1 = high
        values, indices = np.unique(volume.blocks[y0:y1 + 1, z0:z1 + 1, x0:x1 + 1], return_inverse=True)
        result = {'target': target, 'min': list(low), 'max': list(high),
                  'coordinates': 'Original-local xyz, inclusive. Exact block states; no sampling.',
                  'order': 'x fastest, then z, then y; indices address this response palette',
                  'palette': [volume.palette[int(i)] for i in values], 'blocks': indices.ravel().tolist()}
        if len(json.dumps(result).encode()) > MAX_REGION_BYTES:
            raise ValueError(f'Region response exceeds {MAX_REGION_BYTES // 1024} KiB; inspect a smaller region')
        return result

    def _target(self, target):
        if target not in ('source', 'candidate'):
            raise ValueError('target must be source or candidate')
        volume = self.load_source() if target == 'source' else self.candidate
        if volume is None:
            raise ValueError('No candidate available; call preview_plan successfully first in this pass')
        return volume

    def sample_surface(self, min, max, step=1, target='source'):
        """Exact per-column heights over an inclusive x/z rectangle, every step columns."""
        def point(value, name):
            if not isinstance(value, list) or len(value) != 2 or any(type(v) is not int for v in value):
                raise ValueError(f'{name} must be two integer local [x, z] coordinates')
            if not (0 <= value[0] < self.dimensions[0] and 0 <= value[1] < self.dimensions[2]):
                raise ValueError(f'{name} {value} is outside the selection x/z bounds')
            return value
        (x0, z0), (x1, z1) = point(min, 'min'), point(max, 'max')
        if x0 > x1 or z0 > z1:
            raise ValueError('min exceeds max')
        if type(step) is not int or not 1 <= step <= MAX_SURFACE_STEP:
            raise ValueError(f'step must be an integer from 1 to {MAX_SURFACE_STEP}')
        xs, zs = range(x0, x1 + 1, step), range(z0, z1 + 1, step)
        if len(xs) * len(zs) > MAX_SURFACE_COLUMNS:
            raise ValueError(f'Sample at most {MAX_SURFACE_COLUMNS} columns per call; raise step or split the rectangle')
        volume = self._target(target)
        from .spatial import palette_tables
        occupied_table, support_table = palette_tables(volume.palette)
        liquid_table = np.array([parse_state(state)[0] in {'minecraft:water', 'minecraft:lava', 'minecraft:bubble_column'}
                                 for state in volume.palette])
        columns = volume.blocks[:, z0:z1 + 1:step, x0:x1 + 1:step]  # (y, z, x)
        height = columns.shape[0]

        def highest(mask):
            return np.where(mask.any(axis=0), height - 1 - np.argmax(mask[::-1], axis=0), -1)
        occupied = occupied_table[columns]
        top, ground, liquid = highest(occupied), highest(support_table[columns]), highest(liquid_table[columns])
        palette, ids = [], {}

        def index(y, z, x):
            if y < 0:
                return -1
            state = volume.palette[int(columns[y, z, x])]
            if state not in ids:
                ids[state] = len(palette)
                palette.append(state)
            return ids[state]
        rows = []
        for zi, z in enumerate(zs):
            for xi, x in enumerate(xs):
                t, g = int(top[zi, xi]), int(ground[zi, xi])
                clearance = -1
                if g >= 0:
                    above = occupied[g + 1:, zi, xi]
                    clearance = int(np.argmax(above)) if above.any() else height - g - 1
                rows.append([x, z, t, index(t, zi, xi), g, index(g, zi, xi), int(liquid[zi, xi]), clearance])
        result = {'target': target, 'min': [x0, z0], 'max': [x1, z1], 'step': step,
                  'coordinates': 'Original-local x/z, inclusive; heights are local y. Exact, not interpolated.',
                  'fields': ['x', 'z', 'top_y', 'top_block', 'ground_y', 'ground_block', 'liquid_y', 'air_above_ground'],
                  'notes': ['-1 means absent in this column (block index -1: none).',
                            'top is the highest non-air block, including leaves, plants and fluids.',
                            'ground is the highest block a player could stand on by a conservative name heuristic '
                            '(no leaves, plants, fluids, slabs, stairs, fences or modded blocks); it does not prove solid ground below.',
                            'liquid_y is the highest water, lava or bubble column, covered or not.',
                            'air_above_ground counts air cells above ground up to the first occupied cell or the selection top.'],
                  'palette': palette, 'columns': rows}
        if len(json.dumps(result).encode()) > MAX_REGION_BYTES:
            raise ValueError(f'Surface response exceeds {MAX_REGION_BYTES // 1024} KiB; sample fewer columns')
        return result

    def preview_plan(self, plan, include_image=False, camera=None):
        if type(include_image) is not bool:
            raise ValueError('include_image must be a boolean')
        from .render import normalize_camera
        if camera is not None:
            # Fail before spending a preview on a camera that cannot render.
            camera = normalize_camera(camera, self.dimensions)
        if self.preview_calls >= MAX_PREVIEWS:
            raise ValueError(f'Candidate preview budget exhausted ({MAX_PREVIEWS} per pass); use validate_plan for remaining checks')
        self.preview_calls += 1
        # Never leave a stale candidate available after a failed preview.
        self.candidate = None
        source = self.load_source()
        candidate, report = apply_plan(source, plan, max_operations=self.max_operations)
        from .spatial import fit_diagnostics
        result = {'valid': True, 'replay_target': 'ORIGINAL source',
                  'fit_diagnostics': fit_diagnostics(source, candidate),
                  'report': {k: v for k, v in report.items() if k != 'operation_counts'},
                  'previews_remaining': MAX_PREVIEWS - self.preview_calls,
                  'note': 'Fit warnings are heuristics, not physics or accessibility proof. Candidate inspection now refers to this plan.'}
        if self.lecterns:
            from .lecterns import lectern_check
            result['lectern_check'] = lectern_check(source, candidate, self.lecterns)
        if include_image or camera is not None:
            from .render import ISOMETRIC_VIEW, PREVIEW_HEIGHT, PREVIEW_WIDTH, render_camera, render_preview
            rendered = {}
            with tempfile.TemporaryDirectory(prefix='worldprompt-preview-') as temporary:
                if camera is None:
                    path = render_preview(candidate, Path(temporary) / 'candidate.png', width=PREVIEW_WIDTH, height=PREVIEW_HEIGHT, report=rendered)
                else:
                    path = render_camera(candidate, Path(temporary) / 'candidate.png', camera, report=rendered)
                result['_image'] = base64.b64encode(Path(path).read_bytes()).decode('ascii')
            view = ISOMETRIC_VIEW if camera is None else (
                f"perspective from eye {rendered['camera']['position']} looking along {rendered['forward']} "
                f"(yaw {rendered['camera']['yaw']:g}, pitch {rendered['camera']['pitch']:g}, fov {rendered['camera']['fov']:g}); "
                "gizmo bottom right shows +X east, +Y up, +Z south; faces crossing the eye plane are omitted")
            result['image_description'] = ('CANDIDATE after replay on ORIGINAL source; X east / Y up / Z south; ' + view +
                                           '. Cube rendering; use inspect_region for exact states.')
            result['image'] = {'view': 'isometric' if camera is None else 'camera', 'sampling_stride': rendered['stride'],
                               **({'camera': rendered['camera'], 'look_direction': rendered['forward'],
                                   'faces_drawn': rendered['faces_drawn']} if camera is not None else {})}
        self.candidate = candidate
        return result
