"""Owned loopback HTTP calls for the isolated bridge experiment.

No public AIOS contracts, engine lifecycle authority, retries, proxies or
redirect following. Probe results describe received endpoint/model metadata;
they do not identify a Strata implementation or infer generation capabilities.
A complete valid response after cancellation remains ANSWERED. The caller owns
the turn-level output and subsequent-submission fence.
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import math
import socket
import threading
import time

from .contracts import EngineConfig, EngineResult, endpoint

MAX_MESSAGES = 16 * 1024
MAX_RESPONSE = 256 * 1024
STATS = frozenset(('total_duration', 'load_duration', 'prompt_eval_count',
                  'prompt_eval_cached_count', 'prompt_eval_duration', 'eval_count', 'eval_duration'))


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _utf8(raw):
    if raw is None:
        return None
    try:
        return raw.decode('utf-8')
    except UnicodeError:
        return None


def _load(raw):
    if type(raw) is not bytes or len(raw) > MAX_RESPONSE:
        raise ValueError('response-size')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('response-duplicate-key')
            result[key] = value
        return result
    def constant(_value):
        raise ValueError('response-nonfinite')
    try:
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, RecursionError, OverflowError) as exc:
        raise ValueError('response-json') from exc
    remaining = 16384
    def bounded(value, depth):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 16:
            raise ValueError('response-complexity')
        if type(value) is dict:
            for item in value.values():
                bounded(item, depth + 1)
        elif type(value) is list:
            for item in value:
                bounded(item, depth + 1)
        elif type(value) is float and not math.isfinite(value):
            raise ValueError('response-nonfinite')
    bounded(result, 0)
    if type(result) is not dict:
        raise ValueError('response-object')
    return result


def _integer(value):
    return type(value) is int and 0 <= value < 1 << 63


def _text(value):
    return (type(value) is str and bool(value.strip())
            and not any(ord(c) < 32 and c not in '\n\t' for c in value))


def _config(engine):
    if type(engine) is not EngineConfig:
        raise ValueError('engine-config')
    # Frozen dataclasses still have mutable dictionaries. Recheck fields using
    # the admitted class rather than an instance-shadowed record method.
    checked = EngineConfig(**EngineConfig.record(engine))
    if checked.kind not in ('ollama', 'openai'):
        raise ValueError('engine-kind-unsupported')
    return checked


def _body(engine, messages):
    if type(messages) is not list or not 1 <= len(messages) <= 32:
        raise ValueError('messages')
    for message in messages:
        if (type(message) is not dict or set(message) != {'role', 'content'}
                or type(message['role']) is not str
                or message['role'] not in ('system', 'user', 'assistant')
                or not _text(message['content'])):
            raise ValueError('messages')
    if not any(m['role'] == 'user' for m in messages) or len(_encoded(messages)) > MAX_MESSAGES:
        raise ValueError('messages-size')
    common = {'model': engine.model, 'messages': messages, 'stream': False}
    if engine.kind == 'ollama':
        common.update(think=False, options={'num_predict': engine.max_tokens})
    else:
        common.update(max_tokens=engine.max_tokens, temperature=0)
    return _encoded(common)


class _TransportError(Exception):
    def __init__(self, reason, raw=None, status=None):
        super().__init__(reason)
        self.raw, self.status = raw, status


class _OwnedSocket(socket.socket):
    """Bound individual reads without poisoning HTTPResponse's buffered reader.

Windows select-backed reads need not wake immediately after another thread's
shutdown. Keep read timeouts inside recv_into, preserving partial header/body
state until the same request completes, is cancelled, or reaches its deadline.
"""
    def recv_into(self, buffer, nbytes=0, flags=0):
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('owned-http-deadline')
            cancelled = self.cancel is not None and self.cancel.is_set()
            # Already queued complete bytes may still yield a valid late result.
            self.settimeout(min(.001 if cancelled else .05, remaining))
            try:
                return super().recv_into(buffer, nbytes, flags)
            except socket.timeout:
                if self.cancel is not None and self.cancel.is_set():
                    raise OSError('owned-http-cancelled')


def _exchange(engine, path, body, cancel, deadline):
    """One owned connection, bounded body and overall wall-clock deadline."""
    host, port, _base = endpoint(engine.base_url)
    connection = http.client.HTTPConnection(host, port, timeout=max(.001, deadline - time.monotonic()))
    def connect(address, timeout, source_address):
        if address != (host, port) or source_address is not None:
            raise ValueError('owned-http-address')
        owned = _OwnedSocket(socket.AF_INET, socket.SOCK_STREAM)
        owned.deadline, owned.cancel = deadline, cancel
        try:
            owned.settimeout(timeout)
            owned.connect(address)
            owned.settimeout(min(.05, max(.001, deadline - time.monotonic())))
            return owned
        except BaseException:
            owned.close()
            raise
    connection._create_connection = connect
    raw, status = None, None
    try:
        connection.connect()
        connection.auto_open = 0
        if time.monotonic() >= deadline:
            raise _TransportError('deadline')
        if cancel is not None and cancel.is_set():
            raise _TransportError('cancelled-after-dispatch')
        connection.request('GET' if body is None else 'POST', path, body=body,
            headers={'Content-Type': 'application/json', 'Connection': 'close'})
        response = connection.getresponse()
        try:
            status = response.status
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise _TransportError('response-size', status=status)
            if response.length not in (None, 0):
                raise _TransportError('response-truncated', raw, status)
            if time.monotonic() >= deadline:
                raise _TransportError('deadline', raw, status)
            if status != 200:
                raise _TransportError('http-status', raw, status)
            if (response.getheader('Content-Type', '').split(';', 1)[0].strip().lower()
                    != 'application/json'
                    or response.getheader('Content-Encoding', 'identity').strip().lower() != 'identity'):
                raise _TransportError('http-content-type-or-encoding', raw, status)
            # Do not discard an already complete valid response merely because
            # the turn cancellation raced with reading its final bytes.
            return raw, status
        finally:
            response.close()
    except _TransportError:
        raise
    except (OSError, http.client.HTTPException, ValueError) as exc:
        reason = ('deadline' if time.monotonic() >= deadline else
                  'cancelled-after-dispatch' if cancel is not None and cancel.is_set() else 'http-transport')
        raise _TransportError(reason, raw, status) from exc
    finally:
        connection.close()


def _usage(value):
    if value is None:
        return None
    if type(value) is not dict:
        raise ValueError('response-usage')
    def validate(item, depth=0):
        if depth > 4:
            raise ValueError('response-usage')
        if type(item) is dict:
            for nested in item.values():
                validate(nested, depth + 1)
        elif item is not None and not _integer(item):
            raise ValueError('response-usage')
    validate(value)
    for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        if key in value and not _integer(value[key]):
            raise ValueError('response-usage')
    if all(key in value for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')):
        if value['prompt_tokens'] + value['completion_tokens'] != value['total_tokens']:
            raise ValueError('response-usage-total')
    return copy.deepcopy(value)


def _answer(engine, value):
    if value.get('model') != engine.model:
        raise ValueError('response-model')
    if engine.kind == 'ollama':
        if value.get('done') is not True or value.get('done_reason') != 'stop':
            raise ValueError('response-finish-' + ('length' if value.get('done_reason') == 'length' else 'unsupported'))
        message = value.get('message')
        if 'created_at' in value and (not _text(value['created_at']) or len(value['created_at']) > 128):
            raise ValueError('response-created-at')
        for key in STATS & set(value):
            if not _integer(value[key]):
                raise ValueError('response-statistic')
        if set(value) - {'model', 'created_at', 'message', 'done', 'done_reason'} - STATS:
            raise ValueError('response-fields')
        usage = {key: value[key] for key in ('prompt_eval_count', 'eval_count') if key in value} or None
    else:
        choices = value.get('choices')
        if type(choices) is not list or len(choices) != 1 or type(choices[0]) is not dict:
            raise ValueError('response-choices')
        choice = choices[0]
        if type(choice.get('index')) is not int or choice['index'] != 0:
            raise ValueError('response-choice-index')
        if choice.get('finish_reason') != 'stop':
            raise ValueError('response-finish-' + ('length' if choice.get('finish_reason') == 'length' else 'unsupported'))
        if choice.get('logprobs') is not None or set(choice) - {'index', 'message', 'finish_reason', 'logprobs'}:
            raise ValueError('response-choice-fields')
        if 'created' in value and not _integer(value['created']):
            raise ValueError('response-created')
        message = choice.get('message')
        usage = _usage(value.get('usage'))
    if (type(message) is not dict or set(message) - {'role', 'content', 'refusal'}
            or message.get('refusal') is not None or message.get('role') != 'assistant'
            or not _text(message.get('content'))):
        raise ValueError('response-message')
    return message['content'], usage


def request(engine: EngineConfig, messages: list[dict], cancel: threading.Event) -> EngineResult:
    """Invalid input raises before I/O; a started uncertain attempt is UNKNOWN."""
    engine = _config(engine)
    body = _body(engine, messages)
    if not isinstance(cancel, threading.Event):
        raise ValueError('cancel-event')
    started = time.monotonic()
    def result(outcome, text=None, raw=None, value=None, error=None, usage=None, status=None):
        return EngineResult(engine.name, engine.kind, engine.model, outcome, text,
            max(0.0, time.monotonic() - started), _sha(body), _sha(raw) if raw is not None else None,
            copy.deepcopy(value), error, copy.deepcopy(usage), body.decode('utf-8'), _utf8(raw), status)
    if cancel.is_set():
        return result('NOT_STARTED', error='cancelled-before-dispatch')
    path = '/api/chat' if engine.kind == 'ollama' else '/v1/chat/completions'
    raw, value, status = None, None, None
    try:
        raw, status = _exchange(engine, path, body, cancel, started + engine.timeout_s)
        value = _load(raw)
        text, usage = _answer(engine, value)
        return result('ANSWERED', text=text, raw=raw, value=value, usage=usage, status=status)
    except _TransportError as exc:
        raw = exc.raw
        if raw is not None:
            try:
                value = _load(raw)
            except ValueError:
                pass
        return result('UNKNOWN', raw=raw, value=value, error=str(exc), status=exc.status)
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        return result('UNKNOWN', raw=raw, value=value,
            error='response-invalid' if type(exc) is not ValueError else str(exc)[:128], status=status)


def probe(engine: EngineConfig, cancel: threading.Event | None = None) -> dict:
    """Observe endpoint metadata and exact listed model; never infer capabilities."""
    engine = _config(engine)
    started = time.monotonic()
    record = {'schema_version': 1, 'name': engine.name, 'kind': engine.kind, 'model': engine.model,
        'base_url': engine.base_url, 'outcome': 'UNKNOWN', 'model_present': False,
        'server_version': None, 'model_digest': None, 'responses': [], 'error': None, 'elapsed_s': 0.0}
    paths = ('/api/version', '/api/tags') if engine.kind == 'ollama' else ('/health', '/v1/models')
    try:
        for path in paths:
            if cancel is not None and cancel.is_set():
                raise ValueError('probe-cancelled')
            raw, status = _exchange(engine, path, None, cancel, started + engine.timeout_s)
            record['responses'].append({'endpoint': path, 'status': status,
                'response_sha256': _sha(raw), 'response_body': _utf8(raw), 'raw_response': None})
            record['responses'][-1]['raw_response'] = _load(raw)
        first, listed = [item['raw_response'] for item in record['responses']]
        if engine.kind == 'ollama':
            if set(first) != {'version'} or not _text(first['version']) or len(first['version']) > 128:
                raise ValueError('probe-version')
            record['server_version'] = first['version']
            models = listed.get('models')
        else:
            if 'status' in first and first['status'] != 'ok':
                raise ValueError('probe-health')
            if listed.get('object') != 'list':
                raise ValueError('probe-model-list')
            models = listed.get('data')
        if type(models) is not list or len(models) > 128:
            raise ValueError('probe-model-list')
        matches = []
        for model in models:
            if type(model) is not dict:
                raise ValueError('probe-model')
            keys = ('name', 'model') if engine.kind == 'ollama' else ('id',)
            if not any(type(model.get(key)) is str and _text(model[key]) for key in keys):
                raise ValueError('probe-model-id')
            for key in ('created', 'size'):
                if key in model and not _integer(model[key]):
                    raise ValueError('probe-model-number')
            if any(model.get(key) == engine.model for key in keys):
                matches.append(model)
        if len(matches) != 1:
            raise ValueError('probe-model-missing-or-ambiguous')
        record['model_present'] = True
        if engine.kind == 'ollama':
            digest = matches[0].get('digest')
            if type(digest) is not str or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('probe-model-digest')
            record['model_digest'] = digest
        record['outcome'] = 'CONFIRMED'
    except _TransportError as exc:
        record['error'] = str(exc)
        if exc.raw is not None:
            try:
                value = _load(exc.raw)
            except ValueError:
                value = None
            record['responses'].append({'endpoint': paths[len(record['responses'])], 'status': exc.status,
                'response_sha256': _sha(exc.raw), 'response_body': _utf8(exc.raw), 'raw_response': value})
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        record['error'] = str(exc)[:128]
    record['elapsed_s'] = max(0.0, time.monotonic() - started)
    return record
