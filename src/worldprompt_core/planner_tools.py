"""Private, bounded stdio MCP tools; no live world, network or arbitrary file access."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from .block_registry import (BlockCatalog, catalog_version, current_catalog, get_block, list_blocks,
                             read_catalog_file, using_catalog)
from .core import validate_plan
from . import wsl
from .limits import (DEFAULT_CAMERA_FOV, MAX_BLOCK_PAGE, MAX_BLOCK_QUERY_CHARS, MAX_CAMERA_DISTANCE, MAX_CAMERA_FOV,
                     MAX_INSPECT_BLOCKS, MAX_PLAN_BYTES, MAX_PREVIEWS, MAX_REGION_BYTES, MAX_STATE_CHARS,
                     MAX_SURFACE_COLUMNS, MAX_SURFACE_STEP, MAX_TOOL_CALLS, MIN_CAMERA_FOV)
from .operations import field_schema

TOOL_NAMES = ['list_blocks', 'get_block', 'validate_plan', 'inspect_region', 'sample_surface', 'preview_plan']


def server_command(job_dir, config):
    """Launch this installation, including frozen Core and Windows CLI from WSL."""
    executable = sys.executable
    if getattr(sys, 'frozen', False):
        if os.name == 'nt':
            executable = str(Path(executable).with_name('WorldPrompt-Core-Console.exe'))
        command = [executable, 'planner-tools', '--request', str(job_dir / 'request.json')]
    else:
        # An explicit import root also supports an uninstalled source checkout.
        bootstrap = 'import sys; sys.path.insert(0, ' + repr(str(Path(__file__).resolve().parents[1])) + '); from worldprompt_core.__main__ import main; sys.exit(main())'
        command = [executable, '-c', bootstrap, 'planner-tools', '--request', str(job_dir / 'request.json')]
    # A live job's catalog is authoritative, including in this subprocess. Pass it explicitly when
    # this scope has one: a missing sidecar is a broken job, not evidence of a legacy request, and
    # planning against bundled vanilla would silently disagree with final validation.
    if current_catalog() is not None:
        catalog_path = Path(job_dir) / 'block-catalog.json'
        if not catalog_path.is_file():
            raise FileNotFoundError(f'This job has a live block catalog but no sidecar at {catalog_path}.')
        command.extend(['--catalog', str(catalog_path)])
    if wsl.windows_cli(config):
        command = wsl.wsl_exec(command)
    return {'command': command[0], 'args': command[1:]}


def tool_definitions():
    point = field_schema('point')
    column = {'type': 'array', 'items': {'type': 'integer', 'minimum': 0}, 'minItems': 2, 'maxItems': 2}
    def tool(name, description, properties, required=()):
        return {'name': name, 'description': description,
                'inputSchema': {'type': 'object', 'properties': properties,
                                'required': list(required), 'additionalProperties': False},
                'annotations': {'readOnlyHint': True, 'destructiveHint': False,
                                'idempotentHint': True, 'openWorldHint': False}}
    version = catalog_version()
    return [tool('list_blocks', f'Search or paginate all Minecraft {version} block IDs.',
                 {'query': {'type': 'string', 'maxLength': MAX_BLOCK_QUERY_CHARS}, 'offset': {'type': 'integer', 'minimum': 0},
                  'limit': {'type': 'integer', 'minimum': 1, 'maximum': MAX_BLOCK_PAGE}}),
            tool('get_block', f'Get accepted properties, values and defaults for a Minecraft {version} block.',
                 {'block': {'type': 'string', 'maxLength': MAX_STATE_CHARS}}, ['block']),
            tool('validate_plan', 'Check a complete candidate plan before final submission. Fix errors and check again. Uses this job’s dimensions and limits.',
                 {'plan': {'type': 'object'}}, ['plan']),
            tool('inspect_region', f'Read exact states in an inclusive local xyz box, column or slice (up to {MAX_INSPECT_BLOCKS} blocks, {MAX_REGION_BYTES // 1024} KiB). Source is the original capture; candidate is the last successful preview_plan in this pass.',
                 {'min': point, 'max': point, 'target': {'type': 'string', 'enum': ['source', 'candidate'], 'default': 'source'}}, ['min', 'max']),
            tool('sample_surface', f'Exact per-column top, standable-ground and liquid heights plus air above ground over an inclusive local [x, z] rectangle, every step columns (up to {MAX_SURFACE_COLUMNS} columns). Source is the original capture; candidate is the last successful preview_plan in this pass.',
                 {'min': column, 'max': column, 'step': {'type': 'integer', 'minimum': 1, 'maximum': MAX_SURFACE_STEP, 'default': 1},
                  'target': {'type': 'string', 'enum': ['source', 'candidate'], 'default': 'source'}}, ['min', 'max']),
            tool('preview_plan', f'Optionally apply a complete plan privately to ORIGINAL source and return fit diagnostics. At most {MAX_PREVIEWS} previews per pass. Request an image only if needed: the default is a fixed isometric view from above the south-east; a camera renders a perspective view from your chosen eye point and implies an image. Does not change Minecraft or submit the plan.',
                 {'plan': {'type': 'object'}, 'include_image': {'type': 'boolean', 'default': False},
                  'camera': {'type': 'object', 'additionalProperties': False, 'required': ['position', 'yaw', 'pitch'], 'properties': {
                      'position': {'type': 'array', 'items': {'type': 'number'}, 'minItems': 3, 'maxItems': 3,
                                   'description': f'Local xyz eye point; may lie outside the volume (within {MAX_CAMERA_DISTANCE} blocks).'},
                      'yaw': {'type': 'number', 'minimum': -360, 'maximum': 360,
                              'description': 'Minecraft degrees: 0 looks south (+z), 90 west (-x), 180 north, -90 east.'},
                      'pitch': {'type': 'number', 'minimum': -90, 'maximum': 90,
                                'description': 'Minecraft degrees: 0 level, 90 straight down, -90 straight up.'},
                      'fov': {'type': 'number', 'minimum': MIN_CAMERA_FOV, 'maximum': MAX_CAMERA_FOV, 'default': DEFAULT_CAMERA_FOV,
                              'description': 'Horizontal field of view in degrees.'}}}}, ['plan'])]


def lectern_positions(request, dimensions):
    """Prompt lectern positions from the planner request, for preview diagnostics only."""
    positions = []
    for item in request.get('lectern_instructions') or []:
        position = item.get('position') if isinstance(item, dict) else None
        if (isinstance(position, list) and len(position) == 3 and all(type(n) is int for n in position)
                and all(0 <= n < limit for n, limit in zip(position, dimensions))):
            positions.append(position)
    return positions


class PlannerTools:
    def __init__(self, request, directory=None, catalog: BlockCatalog | None = None):
        self.dimensions = request['volume']['dimensions']
        self.max_operations = request['limits']['max_operations']
        self.max_plan_bytes = request['limits'].get('max_plan_bytes', MAX_PLAN_BYTES)
        self.calls = 0
        self.catalog = catalog
        from .spatial_tools import SpatialTools
        self.spatial = SpatialTools(directory, self.dimensions, self.max_operations,
                                    lecterns=lectern_positions(request, self.dimensions))

    def call(self, name, arguments):
        with using_catalog(self.catalog):
            return self._call(name, arguments)

    def definitions(self):
        """The tool list, described by this server's own catalog."""
        with using_catalog(self.catalog):
            return tool_definitions()

    def _call(self, name, arguments):
        self.calls += 1
        if self.calls > MAX_TOOL_CALLS:
            raise ValueError(f'Planner tool call budget exhausted ({MAX_TOOL_CALLS} calls per pass)')
        if not isinstance(arguments, dict):
            raise ValueError('Tool arguments must be an object')
        if name == 'list_blocks':
            return list_blocks(**arguments)
        if name == 'get_block':
            return get_block(**arguments)
        if name == 'validate_plan' and set(arguments) == {'plan'}:
            return validate_plan(arguments['plan'], self.dimensions, self.max_operations, self.max_plan_bytes)
        if name == 'inspect_region':
            return self.spatial.inspect_region(**arguments)
        if name == 'sample_surface':
            return self.spatial.sample_surface(**arguments)
        if name == 'preview_plan':
            if set(arguments) - {'plan', 'include_image', 'camera'} or 'plan' not in arguments:
                raise ValueError('Invalid preview_plan arguments')
            # Enforce exactly the submission limits before allocating a candidate.
            self.spatial.candidate = None
            checked = validate_plan(arguments['plan'], self.dimensions, self.max_operations, self.max_plan_bytes)
            if not checked['valid']:
                return checked
            return self.spatial.preview_plan(**arguments)
        raise ValueError('Unknown tool or invalid arguments')


def serve(request_path, input_stream=None, output_stream=None, catalog_path=None):
    input_stream = input_stream or sys.stdin.buffer
    output_stream = output_stream or sys.stdout
    request = json.loads(Path(request_path).read_text(encoding='utf-8'))
    catalog = read_catalog_file(catalog_path) if catalog_path is not None else None
    server = PlannerTools(request, Path(request_path).parent, catalog)
    return _serve_loop(server, input_stream, output_stream)


def _serve_loop(server, input_stream, output_stream):
    # The server owns the catalog and scopes every call; see PlannerTools.call and definitions.
    limit = 1024 * 1024
    while raw := input_stream.readline(limit + 1):
        if len(raw) > limit:
            return 1
        identifier = None
        try:
            message = json.loads(raw)
            if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                raise ValueError('Invalid JSON-RPC request')
            if 'id' not in message:
                continue
            identifier = message['id']
            method = message.get('method')
            if method == 'initialize':
                params = message.get('params', {})
                protocol = params.get('protocolVersion') if isinstance(params, dict) else None
                if protocol not in {'2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25'}:
                    protocol = '2024-11-05'
                result = {'protocolVersion': protocol, 'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'worldprompt', 'version': '1.0'}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': server.definitions()}
            elif method == 'tools/call':
                try:
                    params = message.get('params', {})
                    if not isinstance(params, dict):
                        raise ValueError('Tool params must be an object')
                    value = server.call(params.get('name'), params.get('arguments', {}))
                    image = value.pop('_image', None)
                    result = {'content': [{'type': 'text', 'text': json.dumps(value)}], 'isError': False}
                    if image is not None:
                        result['content'].append({'type': 'image', 'mimeType': 'image/png', 'data': image})
                except (ValueError, TypeError, KeyError) as exc:
                    result = {'content': [{'type': 'text', 'text': str(exc)}], 'isError': True}
                except Exception as exc:
                    # Keep serving after an unexpected tool failure; the call still
                    # counts toward the budget.
                    result = {'content': [{'type': 'text', 'text': f'Tool call failed ({type(exc).__name__}); adjust the arguments or continue without it.'}],
                              'isError': True}
            else:
                output_stream.write(json.dumps({'jsonrpc': '2.0', 'id': identifier,
                                    'error': {'code': -32601, 'message': 'Method not found'}}) + '\n')
                output_stream.flush()
                continue
            response = {'jsonrpc': '2.0', 'id': identifier, 'result': result}
        except (ValueError, TypeError, KeyError, RecursionError):
            response = {'jsonrpc': '2.0', 'id': identifier,
                        'error': {'code': -32600, 'message': 'Invalid request'}}
        output_stream.write(json.dumps(response) + '\n')
        output_stream.flush()
    return 0
