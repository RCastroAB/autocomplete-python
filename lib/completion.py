import os
import io
import re
import sys
import json
import traceback

import jedi


WORD_RE = re.compile(r'\w')
ARGUMENT_RE = re.compile(r'[a-zA-Z0-9_=\*"\']+')


class JediCompletion(object):
    basic_types = {
        'module': 'import',
        'instance': 'variable',
        'statement': 'value',
        'param': 'variable',
    }

    def __init__(self):
        self.default_sys_path = sys.path
        self._input = io.open(sys.stdin.fileno(), encoding='utf-8')
        self.devnull = open(os.devnull, 'w')
        self.stdout, self.stderr = sys.stdout, sys.stderr

    def _get_definition_type(self, definition):
        is_built_in = definition.in_builtin_module
        if definition.type not in ['import', 'keyword'] and is_built_in():
            return 'builtin'
        if definition.type in ['statement'] and definition.name.isupper():
            return 'constant'
        return self.basic_types.get(definition.type, definition.type)

    def _additional_info(self, completion):
        """Provide additional information about the completion object."""
        if not hasattr(completion, '_definition') or completion._definition is None:
            return ''
        if completion.type == 'statement':
            nodes_to_display = ['InstanceElement', 'String', 'Node', 'Lambda',
                                'Number']
            return ''.join(c.get_code() for c in
                           completion._definition.children if type(c).__name__
                           in nodes_to_display).replace('\n', '')
        return ''

    @classmethod
    def _get_top_level_module(cls, path):
        """Recursively walk through directories looking for top level module.

        Jedi will use current filepath to look for another modules at same
        path, but it will not be able to see modules **above**, so our goal
        is to find the higher python module available from filepath.
        """
        _path, _ = os.path.split(path)
        if _path != path and os.path.isfile(os.path.join(_path, '__init__.py')):
            return cls._get_top_level_module(_path)
        return path

    def _generate_signature(self, completion):
        """Generate signature with function arguments.
        """
        if completion.type in ['module'] or not hasattr(completion, 'params'):
            return ''
        return '%s(%s)' % (
            completion.name,
            ', '.join(p.description for p in completion.params if p))

    def _get_call_signatures(self, script):
        """Extract call signatures from jedi.api.Script object in failsafe way.

        Returns:
            Tuple with original signature object, name and value.
        """
        _signatures = []
        try:
            call_signatures = script.get_signatures(*script._pos)
        except KeyError:
            call_signatures = []
        for signature in call_signatures:
            for pos, param in enumerate(signature.params):
                if not param.name:
                    continue
                if param.name == 'self' and pos == 0:
                    continue
                if WORD_RE.match(param.name) is None:
                    continue
                description = re.sub('param ', '', param.description)
                try:
                    name, value = description.split('=')
                except ValueError:
                    name = description
                    value = None
                if name.startswith('*'):
                    continue
                _signatures.append((signature, name, value))
        return _signatures

    def _serialize_completions(self, script, identifier=None, prefix=''):
        """Serialize response to be read from Atom.

        Args:
            script: Instance of jedi.api.Script object.
            identifier: Unique completion identifier to pass back to Atom.
            prefix: String with prefix to filter function arguments.
                Used only when fuzzy matcher turned off.

        Returns:
            Serialized string to send to Atom.
        """
        _completions = []

        for signature, name, value in self._get_call_signatures(script):
            if not self.fuzzy_matcher and not name.lower().startswith(
              prefix.lower()):
                continue
            _completion = {
                'type': 'property',
                'rightLabel': self._additional_info(signature)
            }
            # we pass 'text' here only for fuzzy matcher
            if value:
                _completion['snippet'] = '%s=${1:%s}$0' % (name, value)
                _completion['text'] = '%s=%s' % (name, value)
            else:
                _completion['snippet'] = '%s=$1$0' % name
                _completion['text'] = name
                _completion['displayText'] = name
            if self.show_doc_strings:
                _completion['description'] = signature.docstring()
            else:
                _completion['description'] = self._generate_signature(
                    signature)
            _completions.append(_completion)

        try:
            completions = script.complete(*script._pos, fuzzy=False)
        except KeyError:
            completions = []
        for completion in completions:
            if self.show_doc_strings:
                description = completion.docstring()
            else:
                description = self._generate_signature(completion)
            _completion = {
                'text': completion.name,
                'type': self._get_definition_type(completion),
                'description': description,
                'rightLabel': self._additional_info(completion)
            }
            if any([c['text'].split('=')[0] == _completion['text']
                    for c in _completions]):
              # ignore function arguments we already have
              continue
            _completions.append(_completion)
        return json.dumps({'id': identifier, 'results': _completions})

    def _serialize_methods(self, script, identifier=None, prefix=''):
        _methods = []
        try:
            completions = script.completions()
        except KeyError:
            return []

        for completion in completions:
            if completion.name == '__autocomplete_python':
              instance = completion.parent().name
              break
        else:
            instance = 'self.__class__'

        for completion in completions:
            params = []
            if hasattr(completion, 'params'):
                params = [p.description for p in completion.params
                          if ARGUMENT_RE.match(p.description)]
            if completion.parent().type == 'class':
              _methods.append({
                'parent': completion.parent().name,
                'instance': instance,
                'name': completion.name,
                'params': params,
                'moduleName': completion.module_name,
                'fileName': completion.module_path,
                'line': completion.line,
                'column': completion.column,
              })
        return json.dumps({'id': identifier, 'results': _methods})

    def _serialize_arguments(self, script, identifier=None):
        """Serialize response to be read from Atom.

        Args:
            script: Instance of jedi.api.Script object.
            identifier: Unique completion identifier to pass back to Atom.

        Returns:
            Serialized string to send to Atom.
        """
        seen = set()
        arguments = []
        i = 1
        for _, name, value in self._get_call_signatures(script):
            if not value:
                arg = '${%s:%s}' % (i, name)
            elif self.use_snippets == 'all':
                arg = '%s=${%s:%s}' % (name, i, value)
            else:
              continue
            if name not in seen:
              seen.add(name)
              arguments.append(arg)
            i += 1
        snippet = '%s$0' % ', '.join(arguments)
        return json.dumps({'id': identifier, 'results': [],
                           'arguments': snippet})

    def _top_definition(self, definition):
        for d in definition.goto_assignments():
            if d == definition:
                continue
            if d.type == 'import':
                return self._top_definition(d)
            else:
                return d
        return definition

    def _serialize_definitions(self, definitions, identifier=None):
        """Serialize response to be read from Atom.

        Args:
            definitions: List of jedi.api.classes.Definition objects.
            identifier: Unique completion identifier to pass back to Atom.

        Returns:
            Serialized string to send to Atom.
        """
        _definitions = []
        for definition in definitions:
            if definition.module_path:
                if definition.type == 'import':
                    definition = self._top_definition(definition)
                if not definition.module_path:
                  continue
                _definition = {
                    'text': definition.name,
                    'type': self._get_definition_type(definition),
                    'fileName': definition.module_path,
                    'line': definition.line - 1,
                    'column': definition.column
                }
                _definitions.append(_definition)
        return json.dumps({'id': identifier, 'results': _definitions})

    def _serialize_tooltip(self, definitions, identifier=None):
        _definitions = []
        for definition in definitions:
            if definition.module_path:
                if definition.type == 'import':
                    definition = self._top_definition(definition)
                if not definition.module_path:
                  continue

                description = definition.docstring()
                if description is not None:
                  description = description.strip()
                if not description:
                  description = self._additional_info(definition)
                _definition = {
                    'text': definition.name,
                    'type': self._get_definition_type(definition),
                    'fileName': definition.module_path,
                    'description': description,
                    'line': definition.line - 1,
                    'column': definition.column
                }
                _definitions.append(_definition)
                break
        return json.dumps({'id': identifier, 'results': _definitions})

    def _serialize_usages(self, usages, identifier=None):
      _usages = []
      for usage in usages:
        _usages.append({
          'name': usage.name,
          'moduleName': usage.module_name,
          'fileName': usage.module_path,
          'line': usage.line,
          'column': usage.column,
        })
      return json.dumps({'id': identifier, 'results': _usages})

    def _deserialize(self, request):
        """Deserialize request from Atom.

        Args:
            request: String with raw request from Atom.

        Returns:
            Python dictionary with request data.
        """
        return json.loads(request)

    def _set_request_config(self, config):
        """Sets config values for current request.

        This includes sys.path modifications which is getting restored to
        default value on each request so each project should be isolated
        from each other.

        Args:
            config: Dictionary with config values.
        """
        sys.path = self.default_sys_path
        self.use_snippets = config.get('useSnippets')
        self.show_doc_strings = config.get('showDescriptions', True)
        self.fuzzy_matcher = config.get('fuzzyMatcher', False)
        jedi.settings.case_insensitive_completion = config.get(
            'caseInsensitiveCompletion', True)
        self.extra_paths = []
        for path in config.get('extraPaths', []):
            if path and path not in sys.path:
                self.extra_paths.append(path)

    def _process_request(self, request):
        """Accept serialized request from Atom and write response.
        """
        request = self._deserialize(request)

        self._set_request_config(request.get('config', {}))

        path = self._get_top_level_module(request.get('path', ''))
        if path not in sys.path:
            sys.path.insert(0, path)
        lookup = request.get('lookup', 'completions')

        script = jedi.api.Script(
            code=request['source'],# line=request['line'] + 1,
            # column=request['column'],
            path=request.get('path', ''),
            project=jedi.api.Project(path, added_sys_path=self.extra_paths),
        )
        script._pos = request['line']+1, request['column']



        if lookup == 'definitions':
            return self._write_response(self._serialize_definitions(
                script.goto_assignments(), request['id']))
        if lookup == 'tooltip':
            return self._write_response(self._serialize_tooltip(
                script.goto_assignments(), request['id']))
        elif lookup == 'arguments':
            return self._write_response(self._serialize_arguments(
                script, request['id']))
        elif lookup == 'usages':
            return self._write_response(self._serialize_usages(
                script.usages(), request['id']))
        elif lookup == 'methods':
          return self._write_response(
              self._serialize_methods(script, request['id'],
                                      request.get('prefix', '')))
        else:
            return self._write_response(
                self._serialize_completions(script, request['id'],
                                            request.get('prefix', '')))

    def _write_response(self, response):
        sys.stdout = self.stdout
        sys.stdout.write(response + '\n')
        sys.stdout.flush()

    def watch(self):
        while True:
            try:
                sys.stdout, sys.stderr = self.devnull, self.devnull
                request = self._input.readline()
                if not request:
                    return

                self._process_request(request)
            except Exception:
                sys.stderr = self.stderr
                sys.stderr.write(traceback.format_exc() + '\n')
                sys.stderr.flush()

if __name__ == '__main__':
    if sys.argv[1:]:
      for s in sys.argv[1:]:
        JediCompletion()._process_request(s)
    else:
      JediCompletion().watch()
